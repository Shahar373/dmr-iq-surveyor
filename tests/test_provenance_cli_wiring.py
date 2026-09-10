"""The four commands that can tag a run, wired end to end.

Each of these asserts two things about one command: that `--campaign` reaches
the object that does the work, and that a bad id is refused *before* the
command spends anything -- an SDR opened, ninety seconds of recording, a
server that starts and only fails at the operator's first stop.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fixtures.synthetic import SyntheticTone, write_synthetic_iq_wav
from typer.testing import CliRunner

from dmr_iq_surveyor.cli_app import app
from dmr_iq_surveyor.survey.provenance import SOURCE_DECLARED, load_hardware
from dmr_iq_surveyor.survey.store import connect_survey_database, get_run

runner = CliRunner()

CENTER = 868_000_000
RATE = 200_000

BAND_YAML = """
name: wiring_band
label: "wiring test band"
start_frequency_hz: 867910000
stop_frequency_hz: 868090000
raster_spacings_hz:
  - 12500
  - 6250
detection:
  scan_step_hz: 6250
  integration_width_hz: 12500
  min_p95_channel_snr_db: 9.0
  min_average_channel_snr_db: 4.0
  merge_tolerance_hz: 4000
segment_seconds: 1.0
segment_stride_seconds: 1.0
max_segments: 6
usable_passband_rolloff_db: 3.0
comparison:
  frequency_tolerance_hz: 6250
  snr_delta_db: 3.0
  occupancy_delta_pct: 10.0
  persistence_delta: 0.25
  analyzed_seconds_ratio_limit: 4.0
"""

SITE_YAML = """
site_id: mobile
label: "Mobile receiver"
latitude: 32.05
longitude: 34.79
antenna: "whip"
receiver: "SDRplay RSP1A"
gain_mode: manual
gain: 40.0
lna_state: 2
notes: ""
"""


@pytest.fixture()
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A directory holding profiles where the commands look for them."""
    (tmp_path / "config" / "bands").mkdir(parents=True)
    (tmp_path / "config" / "sites").mkdir(parents=True)
    (tmp_path / "config" / "bands" / "wiring_band.yaml").write_text(BAND_YAML, encoding="utf-8")
    (tmp_path / "config" / "sites" / "mobile.yaml").write_text(SITE_YAML, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _recording(root: Path) -> Path:
    path = root / "stop.wav"
    write_synthetic_iq_wav(
        path,
        sample_rate_hz=RATE,
        center_frequency_hz=CENTER,
        duration_seconds=3.0,
        tones=[SyntheticTone(offset_hz=50_000.0, amplitude=0.3)],
    )
    return path


# -- survey run --------------------------------------------------------------


def test_survey_run_stores_the_campaign_it_was_given(workspace: Path) -> None:
    database = workspace / "db.sqlite3"
    result = runner.invoke(
        app,
        [
            "survey", "run", str(_recording(workspace)),
            "--band", "wiring_band",
            "--site", "mobile",
            "--output", str(workspace / "out"),
            "--database", str(database),
            "--run-id", "r1",
            "--campaign", "  Day1 ",
        ],
    )
    assert result.exit_code == 0, result.output

    connection = connect_survey_database(database)
    try:
        row = get_run(connection, "r1")
        assert row is not None
        assert row["campaign_id"] == "day1"
        # Nothing measured, but the profile it ran under is on record.
        assert load_hardware(row["hardware_json"])["source"] == SOURCE_DECLARED
    finally:
        connection.close()


def test_survey_run_refuses_a_bad_campaign_before_it_analyses_anything(workspace: Path) -> None:
    database = workspace / "db.sqlite3"
    result = runner.invoke(
        app,
        [
            "survey", "run", str(_recording(workspace)),
            "--band", "wiring_band",
            "--site", "mobile",
            "--output", str(workspace / "out"),
            "--database", str(database),
            "--campaign", "Day One",
        ],
    )
    assert result.exit_code == 1
    assert "invalid campaign id" in result.output
    assert not database.exists(), "a rejected run must not create a database"


# -- survey capture ----------------------------------------------------------


def test_survey_capture_checks_the_campaign_before_it_touches_the_radio(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A capture costs ninety seconds. An id checked afterwards is an id
    checked too late."""
    probes: list[str] = []

    def recording_probe(driver: str) -> Any:
        probes.append(driver)
        raise AssertionError("the device must not be probed for a bad campaign id")

    monkeypatch.setattr("dmr_iq_surveyor.cli_survey.probe_soapysdr", recording_probe)
    result = runner.invoke(
        app,
        [
            "survey", "capture", str(workspace / "rec"),
            "--band", "wiring_band",
            "--site", "mobile",
            "--center-frequency", "868000000",
            "--sample-rate", "200000",
            "--duration", "3",
            "--if-gr", "40",
            "--campaign", "Day One",
        ],
    )
    assert result.exit_code == 1
    assert "invalid campaign id" in result.output
    assert probes == []


# -- live stop ---------------------------------------------------------------


class _NeverRuns:
    """Stands in for `LiveSession` so no SDR is opened."""

    captured: list[Any] = []

    def __init__(self, **kwargs: Any) -> None:
        _NeverRuns.captured.append(kwargs["settings"])

    def run(self, **kwargs: Any) -> Any:
        raise AssertionError("the drive must not start in this test")


def test_live_stop_passes_the_campaign_into_the_session(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _NeverRuns.captured = []
    monkeypatch.setattr("dmr_iq_surveyor.cli_live.LiveSession", _NeverRuns)
    result = runner.invoke(
        app,
        [
            "live", "stop",
            "--latitude", "32.05", "--longitude", "34.79",
            "--seconds", "4",
            "--band", "wiring_band",
            "--site", "mobile",
            "--database", str(workspace / "db.sqlite3"),
            "--campaign", "Day1",
        ],
    )
    # The stub refuses to run, so the command fails after the wiring is done.
    assert _NeverRuns.captured, result.output
    assert _NeverRuns.captured[0].campaign_id == "Day1"


def test_live_stop_refuses_a_bad_campaign_before_opening_the_sdr(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(**kwargs: Any) -> Any:
        raise AssertionError("the session must not be built for a bad campaign id")

    monkeypatch.setattr("dmr_iq_surveyor.cli_live.LiveSession", refuse)
    result = runner.invoke(
        app,
        [
            "live", "stop",
            "--latitude", "32.05", "--longitude", "34.79",
            "--seconds", "4",
            "--band", "wiring_band",
            "--site", "mobile",
            "--database", str(workspace / "db.sqlite3"),
            "--campaign", "Day One",
        ],
    )
    assert result.exit_code != 0


# -- web serve ---------------------------------------------------------------


def _serve_args(workspace: Path, campaign: str) -> list[str]:
    return [
        "web", "serve",
        "--host", "127.0.0.1", "--port", "0",
        "--band", "wiring_band",
        "--site", "mobile",
        "--output", str(workspace / "field"),
        "--database", str(workspace / "db.sqlite3"),
        "--token", "s3cret",
        "--campaign", campaign,
    ]


def test_web_serve_puts_the_campaign_into_the_field_settings(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[Any] = []

    def capture_settings(settings: Any, **kwargs: Any) -> None:
        seen.append(settings)

    monkeypatch.setattr("dmr_iq_surveyor.cli_web.serve_forever", capture_settings)
    result = runner.invoke(app, _serve_args(workspace, "  Day1 "))

    assert result.exit_code == 0, result.output
    assert seen, "the server was never started"
    assert seen[0].campaign_id == "day1"


def test_web_serve_refuses_a_bad_campaign_at_startup(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Not at the operator's first stop, out in the field."""

    def never(settings: Any, **kwargs: Any) -> None:
        raise AssertionError("the server must not start with a bad campaign id")

    monkeypatch.setattr("dmr_iq_surveyor.cli_web.serve_forever", never)
    result = runner.invoke(app, _serve_args(workspace, "Day One"))

    assert result.exit_code == 1
    assert "invalid campaign id" in result.output
