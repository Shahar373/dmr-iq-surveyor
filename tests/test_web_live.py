"""The live (moving) survey as the field app drives it.

The pipeline itself is pinned in `test_live_session.py`. What is under test
here is the part the operator touches: fixes arriving from a phone, a drive
that can be started and stopped, bins appearing while it runs, and the
refusals that stop a drive being started in a state where it could only
throw its measurements away.
"""

from __future__ import annotations

import math
import threading
import time
from pathlib import Path

import numpy as np
import pytest
from fixtures.geo_scenario import Transmitter, build_database, seed_run
from fixtures.live_profiles import level_at, tone_chunk, write_profiles

from dmr_iq_surveyor.geo.model import haversine_m
from dmr_iq_surveyor.live import session as live_session
from dmr_iq_surveyor.web import service as web_service
from dmr_iq_surveyor.web.service import FieldService, FieldSettings

RATE = 200_000.0
SITE30_HZ = 867_762_500.0
# One scale for the whole fixture: the seeded stops and the rig's own tone
# are levels of the same transmitter, so they have to be measured on the same
# terms -- a campaign whose stops disagreed about the reference level by 20 dB
# is exactly the common-mode error this project checks for. 45 dB puts the
# far end of the drive at about 17 dB SNR and the near end at 38 dB, which is
# a real spread rather than "detected everywhere".
SITE30 = Transmitter(SITE30_HZ, 32.050, 34.800, reference_level_db=45.0)
CENTER = SITE30_HZ - 70_000.0

class _Probe:
    available = True
    probe_error = ""

    def to_dict(self) -> dict:
        return {"available": True, "resolved_label": "fake SDR"}


class _Rig:
    """The SDR and the car in one object, so the level matches the place.

    The service reads positions from whatever the phone last posted, so the
    rig posts them itself as the clock advances: one read of the stream is
    one slice of wall clock, and the car has moved by the time it returns.
    That is the real coupling, and it is what makes a bin's level mean
    anything.
    """

    def __init__(self, service: FieldService, *, start: tuple[float, float], speed_ms: float):
        self.service = service
        self.start = start
        self.speed_ms = speed_ms
        self.elapsed = 0.0
        self.reads = 0
        self.opened_with = None
        self.closed = False
        self.overflow_count = 0
        self._phase = 0
        self._rng = np.random.default_rng(5)

    def place(self) -> tuple[float, float]:
        metres_per_degree = 111_320.0
        latitude, longitude = self.start
        longitude += (
            self.elapsed * self.speed_ms / (metres_per_degree * math.cos(math.radians(latitude)))
        )
        return latitude, longitude

    def open(self, settings) -> None:
        settings.validate()
        self.opened_with = settings

    def read_stream_chunk(self, max_frames: int) -> np.ndarray:
        self.reads += 1
        self.elapsed += max_frames / RATE
        latitude, longitude = self.place()
        self.service.push_live_position(
            {"latitude": latitude, "longitude": longitude, "accuracy_m": 6.0}
        )
        distance = haversine_m(latitude, longitude, SITE30.latitude, SITE30.longitude)
        chunk = tone_chunk(
            max_frames,
            phase=self._phase,
            offset_hz=SITE30_HZ - CENTER,
            sample_rate_hz=RATE,
            level_db=level_at(distance, reference_level_db=SITE30.reference_level_db),
            rng=self._rng,
        )
        self._phase += max_frames
        return chunk

    def close(self) -> None:
        self.closed = True


def _service(tmp_path: Path, **overrides) -> FieldService:
    database = tmp_path / "db.sqlite3"
    connection = build_database(database)
    for index, (latitude, longitude) in enumerate(
        [(32.045, 34.795), (32.056, 34.806), (32.041, 34.809)]
    ):
        seed_run(
            connection,
            run_id=f"seed_{index}",
            latitude=latitude,
            longitude=longitude,
            transmitters=[SITE30],
            capture_start_utc=f"2026-08-01T{8 + index:02d}:00:00+00:00",
        )
    connection.close()
    band, site = write_profiles(tmp_path / "profiles", center_hz=CENTER)
    base = {
        "database_path": database,
        "output_root": tmp_path / "out",
        "recordings_dir": tmp_path / "rec",
        "band": str(band),
        "site_profile": str(site),
        "center_frequency_hz": CENTER,
        "sample_rate_hz": RATE,
        "live_bin_size_m": 150.0,
        "live_min_windows_per_bin": 2,
        "live_max_windows_per_bin": 4,
        "live_anchor": (32.050, 34.780),
        # Small enough that the in-job final solve finishes in a test.
        "solve_resolution_m": 1_500.0,
        "solve_max_cells": 1_200,
    }
    base.update(overrides)
    return FieldService(FieldSettings(**base))


def _drive(
    service: FieldService,
    monkeypatch: pytest.MonkeyPatch,
    *,
    speed_ms: float = 20.0,
    want_bins: int = 3,
    payload: dict | None = None,
) -> tuple[_Rig, dict]:
    rig = _Rig(service, start=(32.0400, 34.7800), speed_ms=speed_ms)
    monkeypatch.setattr(live_session, "SoapyIqDevice", lambda: rig)
    monkeypatch.setattr(web_service, "probe_soapysdr", lambda driver: _Probe())
    service.push_live_position({"latitude": 32.0400, "longitude": 34.7800, "accuracy_m": 6.0})

    # A test tone is a few hertz wide, so the analysis is sized to this
    # fixture's 200 kS/s rather than to a 5 MS/s field capture: 4096 bins here
    # is 49 Hz, the same order as 65536 bins at 5 MS/s.
    body = {
        "solve_every_bins": 1_000,
        "max_seconds": 60.0,
        "fft_size": 4096,
        "frames_per_window": 8,
    }
    body.update(payload or {})
    job = service.start_live(body)
    deadline = time.monotonic() + 90.0
    while service.live_status()["bin_count"] < want_bins and time.monotonic() < deadline:
        if job.is_terminal():
            break
        time.sleep(0.02)
    job.request_cancel()
    while not job.is_terminal() and time.monotonic() < deadline:
        time.sleep(0.02)
    return rig, job.snapshot()


def test_a_fix_is_timestamped_on_arrival_not_by_the_phone(tmp_path: Path) -> None:
    """A phone's clock can be hours out. Staleness -- the check that stops a
    measurement being placed where the receiver used to be -- has to be
    measured against a clock that cannot jump."""
    service = _service(tmp_path)
    assert service.live_position_age_seconds() is None
    result = service.push_live_position(
        {"latitude": 32.05, "longitude": 34.8, "accuracy_m": 4.0, "at": "1999-01-01T00:00:00Z"}
    )
    assert result["accepted"]
    age = service.live_position_age_seconds()
    assert age is not None and age < 5.0


def test_a_fix_out_of_range_is_refused(tmp_path: Path) -> None:
    service = _service(tmp_path)
    with pytest.raises(ValueError, match="out of range"):
        service.push_live_position({"latitude": 991.0, "longitude": 34.8})
    with pytest.raises(ValueError, match="required numbers"):
        service.push_live_position({"longitude": 34.8})


def test_a_drive_without_gps_is_refused_before_the_sdr_is_opened(tmp_path: Path) -> None:
    """Every window would be dropped for want of a position. Refusing now is
    the difference between a message and a drive that runs for ten minutes
    and writes nothing."""
    service = _service(tmp_path)
    with pytest.raises(ValueError, match="no GPS fix"):
        service.start_live({})


def test_a_drive_with_a_stale_fix_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(tmp_path, live_max_position_age_seconds=1.0)
    service.push_live_position({"latitude": 32.05, "longitude": 34.8})
    later = time.monotonic() + 30.0
    monkeypatch.setattr(web_service.time, "monotonic", lambda: later)
    with pytest.raises(ValueError, match="old"):
        service.start_live({})


def test_a_drive_is_refused_while_another_job_runs(tmp_path: Path) -> None:
    service = _service(tmp_path)
    service.push_live_position({"latitude": 32.05, "longitude": 34.8})
    release = threading.Event()
    started = threading.Event()

    def work(job):
        job.emit("capture", "recording")
        started.set()
        release.wait(5)
        return {}

    service.jobs.submit(kind="capture", label="first", work=work)
    assert started.wait(5)
    try:
        with pytest.raises(RuntimeError, match="already running"):
            service.start_live({})
    finally:
        release.set()


def test_a_drive_writes_bins_as_it_goes_and_solves_at_the_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole point: measurements land while the operator is still
    driving, not at the end -- so an interrupted drive has already
    contributed everything it measured."""
    service = _service(tmp_path)
    rig, snapshot = _drive(service, monkeypatch, want_bins=3)

    assert rig.opened_with is not None, "the SDR must actually be opened"
    assert rig.opened_with.sample_rate_hz == RATE
    assert rig.closed, "the SDR must be released when the drive ends"

    status = service.live_status()
    assert status["bin_count"] >= 3
    assert not status["running"]
    assert status["stats"]["bins_written"] >= 3
    assert status["stats"]["windows_recorded"] >= status["stats"]["bins_written"]
    # Every bin carries where it was measured, not where its grid square is.
    for entry in status["bins"]:
        assert 31.9 < entry["latitude"] < 32.2
        assert 34.7 < entry["longitude"] < 34.9
    assert len(status["trail"]) > 5

    assert snapshot["status"] in {"succeeded", "cancelled"}


def test_the_bins_a_drive_writes_are_ordinary_stops_to_the_rest_of_the_system(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A drive must not need its own code path anywhere downstream. If the
    stop list and the measurement extraction do not see these as stops, the
    binning has bought nothing."""
    service = _service(tmp_path)
    _drive(service, monkeypatch, want_bins=3)

    live_ids = {entry["survey_run_id"] for entry in service.live_status()["bins"]}
    assert live_ids
    stops = {row["survey_run_id"]: row for row in service.stops()}
    assert live_ids <= set(stops), "drive bins must appear in the stop list"
    for run_id in live_ids:
        assert stops[run_id]["gps_latitude"] is not None
        assert stops[run_id]["observation_count"] >= 0
    # And they must be usable as evidence, not merely present.
    assert any(stops[run_id]["detections"] for run_id in live_ids)


def test_a_background_solve_does_not_end_the_drive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Solving runs on its own thread while the stream keeps going. A solve
    that fails, or simply takes longer than the interval, must cost bins at
    worst -- never the drive, which is happening on a road the operator is
    on right now."""
    service = _service(tmp_path)
    _rig, snapshot = _drive(
        service, monkeypatch, want_bins=4, payload={"solve_every_bins": 1}
    )
    assert snapshot["status"] in {"succeeded", "cancelled"}
    assert service.live_status()["bin_count"] >= 4
    deadline = time.monotonic() + 60.0
    while service.live_status()["solving"] and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not service.live_status()["solving"]


def test_live_settings_take_the_campaign_anchor_not_the_first_fix(tmp_path: Path) -> None:
    """Bin indices mean something only relative to an origin. Anchored to
    each drive's own start, the same street would carry different ids on
    different days and every second pass would be written as new evidence."""
    service = _service(tmp_path, live_anchor=(31.5, 34.1))
    settings = service.live_settings({})
    assert (settings.grid_anchor_latitude, settings.grid_anchor_longitude) == (31.5, 34.1)
    fallback = _service(tmp_path / "other", live_anchor=None, map_center=(30.25, 34.9))
    assert fallback.live_settings({}).grid_anchor_latitude == 30.25
