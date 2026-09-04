"""`dmr-surveyor live stop`: a measurement with no recording behind it."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from fixtures.geo_scenario import build_database
from fixtures.live_profiles import level_at, tone_chunk, write_profiles
from typer.testing import CliRunner

from dmr_iq_surveyor.cli_app import app
from dmr_iq_surveyor.live import session as live_session

RATE = 200_000.0
SITE30_HZ = 867_762_500.0
CENTER = SITE30_HZ - 70_000.0

runner = CliRunner()


class _Carrier:
    """A steady carrier, as a stationary receiver 800 m away would hear it."""

    def __init__(self) -> None:
        self.opened_with = None
        self.closed = False
        self.reads = 0
        self._phase = 0
        self._rng = np.random.default_rng(3)

    def open(self, settings) -> None:
        settings.validate()
        self.opened_with = settings

    def read_stream_chunk(self, max_frames: int) -> np.ndarray:
        self.reads += 1
        chunk = tone_chunk(
            max_frames,
            phase=self._phase,
            offset_hz=SITE30_HZ - CENTER,
            sample_rate_hz=RATE,
            level_db=level_at(800.0),
            rng=self._rng,
        )
        self._phase += max_frames
        return chunk

    def close(self) -> None:
        self.closed = True


def _invoke(tmp_path: Path, *extra: str):
    band, site = write_profiles(tmp_path / "profiles", center_hz=CENTER)
    database = tmp_path / "db.sqlite3"
    build_database(database).close()
    return runner.invoke(
        app,
        [
            "live", "stop",
            "--latitude", "32.0500", "--longitude", "34.7900",
            "--band", str(band), "--site", str(site),
            "--database", str(database),
            "--center-frequency", str(CENTER),
            "--sample-rate", str(RATE),
            "--fft-size", "4096",
            *extra,
        ],
    )


def test_a_live_stop_writes_a_measurement_and_no_recording(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    device = _Carrier()
    monkeypatch.setattr(live_session, "SoapyIqDevice", lambda: device)
    result = _invoke(tmp_path, "--seconds", "4")

    assert result.exit_code == 0, result.output
    assert "Written." in result.output
    assert device.opened_with is not None and device.closed
    # The whole point of the mode: nothing on disk but the database.
    written = [
        path
        for path in tmp_path.rglob("*")
        if path.is_file() and path.suffix in {".wav", ".raw", ".iq", ".cf32"}
    ]
    assert written == [], f"a live stop must write no IQ, found {written}"

    from dmr_iq_surveyor.geo.store import connect_geo_database

    connection = connect_geo_database(tmp_path / "db.sqlite3")
    try:
        runs = connection.execute(
            "SELECT survey_run_id, source_path, gps_latitude, segment_count FROM survey_runs"
        ).fetchall()
        observations = connection.execute(
            "SELECT COUNT(*) AS n FROM rf_observations"
        ).fetchone()["n"]
    finally:
        connection.close()
    assert len(runs) == 1
    # No file is named, because there is no file. Saying "live://" is more
    # honest than a path that does not exist.
    assert runs[0]["source_path"].startswith("live://")
    assert runs[0]["gps_latitude"] == pytest.approx(32.0500)
    assert runs[0]["segment_count"] == 4
    assert observations >= 1


def test_an_impossible_averaging_length_is_refused_with_the_two_ways_out(
    tmp_path: Path,
) -> None:
    """The Pi has no gigabytes to spare, and spectra are held in RAM for the
    whole averaging window. Refusing with the arithmetic beats being killed
    by the OOM reaper five minutes in."""
    result = _invoke(tmp_path, "--seconds", "600", "--fft-size", "65536")
    assert result.exit_code == 1
    assert "MB of spectra in RAM" in result.output
    assert "--seconds" in result.output
    assert "--fft-size" in result.output


def test_a_stream_that_delivers_nothing_reports_it_rather_than_claiming_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _Silent(_Carrier):
        def read_stream_chunk(self, max_frames: int) -> np.ndarray:
            self.reads += 1
            return np.empty(0, dtype=np.complex64)

    monkeypatch.setattr(live_session, "SoapyIqDevice", _Silent)
    result = _invoke(tmp_path, "--seconds", "1", "--timeout", "2")
    assert result.exit_code == 1
    assert "Nothing was written" in result.output
