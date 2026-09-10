"""Provenance as the write paths actually produce it.

The point these tests defend: `sites` is current state, not history. It holds
one row per site and `upsert_site` rewrites it, so two runs that share a
`site_id` cannot be told apart by the gain recorded there -- the later run's
value silently becomes the earlier run's too. That is the whole reason a run
carries its own provenance, and why what the radio reported back is kept
apart from what it was asked for.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
from fixtures.synthetic import SyntheticTone, write_synthetic_iq_wav

from dmr_iq_surveyor.capture.core import CaptureSettings, run_capture_and_survey
from dmr_iq_surveyor.capture.device import DeviceSettings
from dmr_iq_surveyor.geo.store import connect_geo_database
from dmr_iq_surveyor.live.session import LiveSession, LiveSettings, Position
from dmr_iq_surveyor.survey.pipeline import DRIVE_VIEW_SUFFIX, DriveViewSettings, run_survey
from dmr_iq_surveyor.survey.profiles import BandProfile, SiteProfile
from dmr_iq_surveyor.survey.provenance import (
    SOURCE_APPLIED,
    SOURCE_NOT_RECORDED,
    SOURCE_REQUESTED,
    if_gain_reading,
    load_hardware,
)
from dmr_iq_surveyor.survey.store import connect_survey_database, get_run

CENTER = 868_000_000.0
RATE = 200_000.0
TONE_OFFSET_HZ = 50_000.0

SITE = SiteProfile(
    site_id="mobile",
    label="Mobile receiver",
    latitude=32.05,
    longitude=34.79,
    receiver="SDRplay RSP1A",
    gain_mode="manual",
    gain=40.0,
    lna_state=2,
)


def _band() -> BandProfile:
    return BandProfile(
        name="test_band",
        label="test",
        start_frequency_hz=CENTER - 90_000.0,
        stop_frequency_hz=CENTER + 90_000.0,
        raster_spacings_hz=[12500.0, 6250.0],
        detection_overrides={
            "scan_step_hz": 6250.0,
            "integration_width_hz": 12500.0,
            "min_p95_channel_snr_db": 9.0,
            "min_average_channel_snr_db": 4.0,
            "merge_tolerance_hz": 4000.0,
        },
        segment_seconds=1.0,
        segment_stride_seconds=1.0,
        max_segments=6,
    )


class _ReadBackDevice:
    """A device that reports its own state back, as `SoapyIqDevice` does.

    `applied_settings` is written in `open()` from what the radio would say it
    ended up at, which is deliberately not what it was asked for: an RSP1A
    asked for IFGR 40 in the field came back at 25.
    """

    def __init__(self, *, applied_if_gain_db: float) -> None:
        self.applied_if_gain_db = applied_if_gain_db
        self.opened_with: DeviceSettings | None = None
        self.applied_settings: dict = {}
        self._phase = 0
        self._rng = np.random.default_rng(3)

    def open(self, settings: DeviceSettings) -> None:
        settings.validate()
        self.opened_with = settings
        self.applied_settings = {
            "sample_rate_hz": settings.sample_rate_hz,
            "center_frequency_hz": settings.center_frequency_hz,
            "bandwidth_hz": settings.sample_rate_hz,
            "agc": settings.agc,
            "gains": {
                "IFGR": self.applied_if_gain_db,
                "RFGR": float(settings.lna_state or 0),
            },
        }

    def read_stream_chunk(self, max_frames: int) -> np.ndarray:
        index = np.arange(self._phase, self._phase + max_frames, dtype=np.float64)
        self._phase += max_frames
        tone = 0.3 * np.exp(2j * np.pi * TONE_OFFSET_HZ * index / RATE)
        noise = self._rng.normal(scale=0.01, size=max_frames) + 1j * self._rng.normal(
            scale=0.01, size=max_frames
        )
        return (tone + noise).astype(np.complex64)

    def close(self) -> None:
        return None


class _SilentDevice(_ReadBackDevice):
    """A device that never reports anything back.

    `run_capture` reads `applied_settings` with a default, so this is the
    honest shape of a driver that exposes no read-back at all.
    """

    def open(self, settings: DeviceSettings) -> None:
        settings.validate()
        self.opened_with = settings
        self.applied_settings = {}


def _capture(tmp_path: Path, *, run_id: str, requested_gain: float, applied_gain: float,
             campaign_id: str | None = None) -> dict:
    return run_capture_and_survey(
        tmp_path / "recordings",
        tmp_path / "surveys" / run_id,
        capture=CaptureSettings(
            center_frequency_hz=CENTER,
            sample_rate_hz=RATE,
            duration_seconds=3.0,
            if_gain_reduction_db=requested_gain,
            lna_state=2,
            agc=False,
        ),
        band=_band(),
        site=SITE,
        device=_ReadBackDevice(applied_if_gain_db=applied_gain),
        run_id=run_id,
        database_path=tmp_path / "db.sqlite3",
        campaign_id=campaign_id,
    )


def _stored_hardware(database: Path, run_id: str) -> dict:
    connection = connect_survey_database(database)
    try:
        row = get_run(connection, run_id)
        assert row is not None
        return load_hardware(row["hardware_json"])
    finally:
        connection.close()


def _stored_campaign(database: Path, run_id: str) -> str | None:
    connection = connect_survey_database(database)
    try:
        row = get_run(connection, run_id)
        assert row is not None
        return row["campaign_id"]
    finally:
        connection.close()


# -- the two stops that share a site -----------------------------------------


def test_two_stops_at_one_site_keep_their_own_applied_gain(tmp_path: Path) -> None:
    """The defect this column exists for.

    Both stops use one site profile, so both point at one `sites` row. That
    row holds the profile's *declared* gain, written by `upsert_site` on
    every run -- it is a statement of intent made before either stop, not a
    record of what either stop ran at. Before a run carried its own
    provenance there was nothing in the database that could tell the two
    apart, and the campaign gain check was comparing every stop against one
    declaration.
    """
    database = tmp_path / "db.sqlite3"
    _capture(tmp_path, run_id="stop_a", requested_gain=40.0, applied_gain=25.0)
    _capture(tmp_path, run_id="stop_b", requested_gain=30.0, applied_gain=30.0)

    connection = connect_survey_database(database)
    try:
        sites = [dict(row) for row in connection.execute("SELECT site_id, gain FROM sites")]
    finally:
        connection.close()

    # Unchanged behaviour, stated rather than assumed: one row, holding the
    # profile's declaration, identical for both stops and equal to neither
    # of the gains the radio actually came back at.
    assert sites == [{"site_id": "mobile", "gain": 40.0}]

    first = _stored_hardware(database, "stop_a")
    second = _stored_hardware(database, "stop_b")

    assert first["source"] == SOURCE_APPLIED
    assert second["source"] == SOURCE_APPLIED
    assert if_gain_reading(first).value == 25.0
    assert if_gain_reading(second).value == 30.0
    # What the `sites` row cannot say: these two stops were not recorded at
    # the same receiver setting.
    assert if_gain_reading(first).value != if_gain_reading(second).value
    # And each keeps what it asked for, separately from what it got.
    assert first["requested"]["if_gain_reduction_db"] == 40.0
    assert second["requested"]["if_gain_reduction_db"] == 30.0


def test_a_device_that_reports_nothing_back_is_recorded_as_requested(tmp_path: Path) -> None:
    database = tmp_path / "db.sqlite3"
    run_capture_and_survey(
        tmp_path / "recordings",
        tmp_path / "surveys" / "quiet",
        capture=CaptureSettings(
            center_frequency_hz=CENTER,
            sample_rate_hz=RATE,
            duration_seconds=3.0,
            if_gain_reduction_db=40.0,
            lna_state=2,
            agc=False,
        ),
        band=_band(),
        site=SITE,
        device=_SilentDevice(applied_if_gain_db=0.0),
        run_id="quiet",
        database_path=database,
    )

    hardware = _stored_hardware(database, "quiet")
    assert hardware["source"] == SOURCE_REQUESTED
    assert hardware["applied"] == {}
    assert if_gain_reading(hardware).source == SOURCE_REQUESTED
    assert if_gain_reading(hardware).value == 40.0


def test_a_capture_carries_its_campaign(tmp_path: Path) -> None:
    database = tmp_path / "db.sqlite3"
    _capture(tmp_path, run_id="stop_a", requested_gain=40.0, applied_gain=25.0,
             campaign_id="2026-09_day1")

    assert _stored_campaign(database, "stop_a") == "2026-09_day1"


# -- analysing a recording ---------------------------------------------------


def _recording(tmp_path: Path) -> Path:
    path = tmp_path / "handed_over.wav"
    write_synthetic_iq_wav(
        path,
        sample_rate_hz=int(RATE),
        center_frequency_hz=int(CENTER),
        duration_seconds=3.0,
        tones=[SyntheticTone(offset_hz=TONE_OFFSET_HZ, amplitude=0.3)],
    )
    return path


def test_an_offline_survey_run_invents_no_receiver_state(tmp_path: Path) -> None:
    """`survey run` on a file analyses a recording it did not make. The site
    profile declares a gain, and that declaration stays in the `sites` row
    where a reader can see it for what it is."""
    database = tmp_path / "db.sqlite3"
    result = run_survey(
        _recording(tmp_path),
        tmp_path / "out",
        band=_band(),
        site=SITE,
        run_id="offline",
        database_path=database,
    )

    assert result["campaign_id"] is None
    hardware = _stored_hardware(database, "offline")
    assert hardware == {}
    assert if_gain_reading(hardware).source == SOURCE_NOT_RECORDED
    # The declaration is still on record, in the place it has always been.
    connection = connect_survey_database(database)
    try:
        row = connection.execute("SELECT gain FROM sites WHERE site_id = 'mobile'").fetchone()
    finally:
        connection.close()
    assert row["gain"] == 40.0


def test_run_json_states_the_campaign_and_how_the_receiver_is_known(tmp_path: Path) -> None:
    run_survey(
        _recording(tmp_path),
        tmp_path / "out",
        band=_band(),
        site=SITE,
        run_id="offline",
        database_path=tmp_path / "db.sqlite3",
        campaign_id="day1",
    )

    manifest = json.loads((tmp_path / "out" / "run.json").read_text(encoding="utf-8"))
    assert manifest["campaign_id"] == "day1"
    assert manifest["hardware"] == {}
    # The pre-existing keys are still there, unchanged.
    assert manifest["survey_run_id"] == "offline"
    assert manifest["band_profile"] == "test_band"


def test_a_drive_view_inherits_the_stop_it_derives_from(tmp_path: Path) -> None:
    """The second reading of one stop is the same radio at the same moment,
    so it must carry the same campaign and the same provenance."""
    database = tmp_path / "db.sqlite3"
    run_survey(
        _recording(tmp_path),
        tmp_path / "out",
        band=_band(),
        site=SITE,
        run_id="stop",
        database_path=database,
        campaign_id="day1",
        drive_view=DriveViewSettings(fft_size=4096, frames_per_window=8, window_seconds=1.0),
    )

    view_id = f"stop{DRIVE_VIEW_SUFFIX}"
    assert _stored_campaign(database, view_id) == "day1"
    assert _stored_hardware(database, view_id) == _stored_hardware(database, "stop")


# -- the drive ---------------------------------------------------------------


class _StraightDrive:
    """A receiver carried east in a straight line, hearing noise.

    A bin is written for the ground it covers whether or not anything is
    detected there, so no signal is needed to exercise what a bin records
    about the radio.
    """

    def __init__(self, *, start: tuple[float, float], east_step_m: float) -> None:
        self.start = start
        self.speed_ms = east_step_m
        self.now = 0.0
        self._rng = np.random.default_rng(11)

    def position(self) -> Position:
        latitude, longitude = self.start
        longitude += (
            self.now * self.speed_ms / (111_320.0 * math.cos(math.radians(latitude)))
        )
        return Position(latitude=latitude, longitude=longitude, at=self.now)

    def clock(self) -> float:
        return self.now

    def open(self, settings: DeviceSettings) -> None:
        settings.validate()

    def read_stream_chunk(self, max_frames: int) -> np.ndarray:
        self.now += max_frames / RATE
        noise = self._rng.normal(scale=0.02, size=max_frames) + 1j * self._rng.normal(
            scale=0.02, size=max_frames
        )
        return noise.astype(np.complex64)

    def close(self) -> None:
        return None


def test_a_drive_records_what_it_asked_for_never_what_it_measured(tmp_path: Path) -> None:
    """A drive commands the radio once and never reads it back, so its bins
    say `requested`. Filing them as applied would give a number nothing
    measured the weight of one that was."""
    database = tmp_path / "drive.sqlite3"
    drive = _StraightDrive(start=(32.05, 34.79), east_step_m=60.0)
    session = LiveSession(
        session_id="d1",
        settings=LiveSettings(
            center_frequency_hz=CENTER,
            sample_rate_hz=RATE,
            window_seconds=1.0,
            bin_size_m=150.0,
            min_windows_per_bin=2,
            max_windows_per_bin=4,
            fft_size=4096,
            frames_per_window=8,
            if_gain_reduction_db=26.0,
            lna_state=8,
            grid_anchor_latitude=32.05,
            grid_anchor_longitude=34.79,
            campaign_id="day1",
        ),
        band=_band(),
        site=SITE,
        database_path=database,
        position_provider=drive.position,
        device=drive,
        clock=drive.clock,
    )
    connection = connect_geo_database(database)
    try:
        session.run(stop=lambda: drive.now >= 12.0, connection=connection)
        rows = [
            dict(row)
            for row in connection.execute(
                "SELECT survey_run_id, campaign_id, hardware_json FROM survey_runs"
            )
        ]
    finally:
        connection.close()

    assert rows, "the drive wrote no bins"
    for row in rows:
        hardware = load_hardware(row["hardware_json"])
        assert row["campaign_id"] == "day1"
        assert hardware["source"] == SOURCE_REQUESTED
        assert hardware["applied"] == {}
        assert if_gain_reading(hardware).value == 26.0
        assert hardware["requested"]["lna_state"] == 8
