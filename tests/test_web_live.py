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


def test_the_live_endpoints_are_reachable_over_http(tmp_path: Path) -> None:
    """Routing, not behaviour: a drive is useless if the phone cannot reach
    it, and the handlers are wired by hand in this server."""
    import json as _json
    import urllib.error
    import urllib.request

    from dmr_iq_surveyor.web.server import create_server

    service_settings = _service(tmp_path).settings
    server = create_server(service_settings, host="127.0.0.1", port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"

    def post(path: str, body: dict) -> tuple[int, dict]:
        request = urllib.request.Request(
            base + path,
            data=_json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.status, _json.loads(response.read().decode() or "{}")
        except urllib.error.HTTPError as error:
            return error.code, _json.loads(error.read().decode() or "{}")

    try:
        status, payload = post("/api/live/position", {"latitude": 32.05, "longitude": 34.8})
        assert status == 200 and payload["accepted"]

        with urllib.request.urlopen(base + "/api/live", timeout=30) as response:
            live = _json.loads(response.read().decode())
        assert live["fix_count"] == 1
        assert live["running"] is False
        assert live["position_age_seconds"] is not None

        status, payload = post("/api/live/position", {"latitude": 999.0, "longitude": 34.8})
        assert status == 400 and "out of range" in payload["error"]

        # The page has to carry the controls, or none of the above is reachable
        # by the person holding the phone.
        with urllib.request.urlopen(base + "/", timeout=30) as response:
            page = response.read().decode()
        assert 'data-panel="drive"' in page
        assert 'id="drive-start"' in page
    finally:
        server.shutdown()
        server.server_close()


def test_starting_a_drive_over_http_without_gps_is_a_client_error(tmp_path: Path) -> None:
    import json as _json
    import urllib.error
    import urllib.request

    from dmr_iq_surveyor.web.server import create_server

    server = create_server(_service(tmp_path).settings, host="127.0.0.1", port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    request = urllib.request.Request(
        base + "/api/live/start",
        data=b"{}",
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            urllib.request.urlopen(request, timeout=30)
        assert excinfo.value.code == 400
        assert "no GPS fix" in _json.loads(excinfo.value.read().decode())["error"]
    finally:
        server.shutdown()
        server.server_close()


def test_solving_is_asked_for_rather_than_automatic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bins are written the whole drive either way; a solve only decides when
    the map catches up. It competes with the receiver for the Pi's cores, so
    the default is to leave the timing to the person in the car."""
    service = _service(tmp_path)
    assert service.settings.live_solve_every_bins == 0

    with pytest.raises(ValueError, match="no drive is running"):
        service.request_live_solve()

    solves: list[str] = []
    real = service._start_live_solve
    monkeypatch.setattr(
        service, "_start_live_solve", lambda job: (solves.append(job.job_id), real(job))[1]
    )
    _rig, snapshot = _drive(service, monkeypatch, want_bins=3, payload={})
    assert snapshot["status"] in {"succeeded", "cancelled"}
    assert service.live_status()["bin_count"] >= 3
    assert solves == [], "nothing may solve on its own while the drive is running"


def test_a_solve_can_be_asked_for_mid_drive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(tmp_path)
    rig = _Rig(service, start=(32.0400, 34.7800), speed_ms=20.0)
    monkeypatch.setattr(live_session, "SoapyIqDevice", lambda: rig)
    monkeypatch.setattr(web_service, "probe_soapysdr", lambda driver: _Probe())
    service.push_live_position({"latitude": 32.0400, "longitude": 34.7800, "accuracy_m": 6.0})
    job = service.start_live(
        {"max_seconds": 60.0, "fft_size": 4096, "frames_per_window": 8}
    )
    deadline = time.monotonic() + 90.0
    while service.live_status()["bin_count"] < 2 and time.monotonic() < deadline:
        if job.is_terminal():
            break
        time.sleep(0.02)

    result = service.request_live_solve()
    assert result["started"] or result["reason"] == "a solve is already running"
    job.request_cancel()
    while not job.is_terminal() and time.monotonic() < deadline:
        time.sleep(0.02)
    while service.live_status()["solving"] and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not service.live_status()["solving"]


def test_the_app_drives_with_adaptive_spans_inside_their_limits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """That a faster drive measures over more road is pinned at the session
    level, where the fixture controls the clock. This fixture cannot: it posts
    fixes through the real endpoint, which stamps them on arrival precisely so
    a phone's own clock cannot be trusted, and its stream is instantaneous. So
    what is checked here is that the app turns the mode on and that the span
    it chooses stays inside the limits whatever the timing looks like."""
    service = _service(tmp_path)
    assert service.settings.live_adaptive_bins
    assert service.live_settings({}).adaptive_bin_size

    _drive(service, monkeypatch, speed_ms=28.0, want_bins=3)
    status = service.live_status()
    assert status["bin_count"] >= 3
    assert 100.0 <= status["stats"]["bin_size_m"] <= 250.0


def test_a_hold_can_only_be_asked_for_mid_drive(tmp_path: Path) -> None:
    service = _service(tmp_path)
    with pytest.raises(ValueError, match="no drive is running"):
        service.request_live_hold({"seconds": 60})


def test_a_hold_request_is_bounded_and_handed_to_the_drive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ten to three hundred seconds, taken at the next window, reported in
    the status the phone polls -- including the countdown a pulled-over
    driver is waiting on."""
    service = _service(tmp_path)
    rig = _Rig(service, start=(32.0400, 34.7800), speed_ms=20.0)
    monkeypatch.setattr(live_session, "SoapyIqDevice", lambda: rig)
    monkeypatch.setattr(web_service, "probe_soapysdr", lambda driver: _Probe())
    service.push_live_position({"latitude": 32.0400, "longitude": 34.7800, "accuracy_m": 6.0})
    job = service.start_live({"max_seconds": 120.0, "fft_size": 4096, "frames_per_window": 8})
    deadline = time.monotonic() + 90.0
    while service.live_status()["bin_count"] < 1 and time.monotonic() < deadline:
        time.sleep(0.02)

    result = service.request_live_hold({"seconds": 3})
    assert result["accepted"]
    assert result["seconds"] == 10.0, "requests under ten seconds are raised to ten"
    status = service.live_status()
    assert status["hold"]["requested"] or status["hold"]["active"]

    # Taken at the next window boundary, then counted down.
    while not service.live_status()["hold"]["active"] and time.monotonic() < deadline:
        time.sleep(0.02)
    active = service.live_status()["hold"]
    assert active["active"]
    assert 0.0 < active["seconds_left"] <= 10.0
    second = service.request_live_hold({"seconds": 60})
    assert not second["accepted"] and "already" in second["reason"]

    job.request_cancel()
    while not job.is_terminal() and time.monotonic() < deadline:
        time.sleep(0.02)
    # The status the phone polls carries both structures whether or not a
    # drive is running, so the page never has to guard against their absence.
    final = service.live_status()
    assert set(final["hold"]) == {"active", "seconds_left", "requested", "holds_written"}
    assert set(final["pull_over"]) == {"suggest", "sites", "channels", "bin", "reason"}
    assert final["hold"]["active"] is False
    assert final["pull_over"]["suggest"] is False, "nothing is suggested once the drive is over"


def test_the_pull_over_hint_names_registry_sites_and_only_them(tmp_path: Path) -> None:
    """A near miss on a control channel the registry knows is a reason to
    stop; a near miss on some other frequency is not. The hint says which."""
    service = _service(tmp_path)
    service._live_cc_index = [(867_762_500.0, "BEE00:37D:1:30"), (866_712_500.0, "BEE00:37D:1:33")]
    service._live_cc_tolerance_hz = 6_250.0
    service._live_bins = [
        {
            "kind": "bin", "survey_run_id": "live_x_+00001_+00001_s",
            "near_threshold": [
                {"frequency_hz": 867_762_500.0, "p95_snr_db": 7.4, "segments_near": 8, "segments_analyzed": 10},
                {"frequency_hz": 868_500_000.0, "p95_snr_db": 8.1, "segments_near": 9, "segments_analyzed": 10},
            ],
        }
    ]
    hint = service._pull_over_hint(running=True)
    assert hint["suggest"] is True
    assert [s["site_key"] for s in hint["sites"]] == ["BEE00:37D:1:30"]
    assert hint["channels"] == 2, "both near misses are counted, one is named"
    assert hint["bin"] == "live_x_+00001_+00001_s"

    service._live_bins[-1]["near_threshold"] = [
        {"frequency_hz": 868_500_000.0, "p95_snr_db": 8.1, "segments_near": 9, "segments_analyzed": 10},
    ]
    hint = service._pull_over_hint(running=True)
    assert hint["suggest"] is False
    assert "none a registry control channel" in hint["reason"]

    assert service._pull_over_hint(running=False)["suggest"] is False


def test_the_hold_endpoint_is_reachable_and_refuses_without_a_drive(tmp_path: Path) -> None:
    import json as _json
    import urllib.error
    import urllib.request

    from dmr_iq_surveyor.web.server import create_server

    server = create_server(_service(tmp_path).settings, host="127.0.0.1", port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    request = urllib.request.Request(
        base + "/api/live/hold", data=b'{"seconds": 60}',
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            urllib.request.urlopen(request, timeout=30)
        assert excinfo.value.code == 400
        assert "no drive" in _json.loads(excinfo.value.read().decode())["error"]
    finally:
        server.shutdown()
        server.server_close()
