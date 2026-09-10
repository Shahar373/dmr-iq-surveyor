"""The digest has to say how each receiver setting is known.

A gain the radio reported back, a gain it was only asked for, and a gain the
site profile declared before the campaign started are three different claims.
A page of text that renders them identically invites the reader to treat a
declaration as a measurement, which is the mistake the provenance column
exists to make impossible.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from dmr_iq_surveyor.survey.profiles import SiteProfile
from dmr_iq_surveyor.survey.provenance import (
    hardware_from_capture_manifest,
    hardware_requested,
)
from dmr_iq_surveyor.survey.store import (
    SurveyRunRecord,
    connect_survey_database,
    import_survey_run,
    upsert_site,
)

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "campaign_digest.py"

SITE = SiteProfile(
    site_id="home",
    label="Home",
    latitude=32.05,
    longitude=34.79,
    gain_mode="manual",
    gain=40.0,
    lna_state=2,
)


def _record(run_id: str, **overrides) -> SurveyRunRecord:
    fields = {
        "survey_run_id": run_id,
        "site_id": SITE.site_id,
        "band_profile": "central_800",
        "source_path": f"/tmp/{run_id}.wav",
        "source_sha256": None,
        "center_frequency_hz": 868_000_000.0,
        "sample_rate_hz": 5_000_000.0,
        "capture_start_utc": "2026-09-01T09:00:00+00:00",
        "capture_time_source": "auxi",
        "requested_start_hz": 866_000_000.0,
        "requested_stop_hz": 870_000_000.0,
        "usable_low_hz": 866_100_000.0,
        "usable_high_hz": 869_900_000.0,
        "coverage_status": "complete",
        "duration_seconds": 90.0,
        "analyzed_seconds": 30.0,
        "segment_count": 30,
        "occupancy_threshold_db": 8.0,
        "detection_settings": {"scan_step_hz": 6250.0},
        "tool_version": "0.10.0",
    }
    fields.update(overrides)
    return SurveyRunRecord(**fields)


def _database(tmp_path: Path) -> Path:
    path = tmp_path / "campaign.sqlite3"
    connection = connect_survey_database(path)
    try:
        upsert_site(connection, SITE)
        read_back = hardware_from_capture_manifest(
            {
                "wav_path": "/tmp/applied_stop.wav",
                "settings": {"if_gain_reduction_db": 40.0, "lna_state": 2, "driver": "sdrplay"},
                "device_settings_applied": {"gains": {"IFGR": 25.0, "RFGR": 2.0}},
            }
        )
        for run_id, campaign, hardware in (
            ("applied_stop", "day1", read_back),
            ("requested_stop", "day1", hardware_requested(if_gain_reduction_db=26.0, lna_state=8)),
            # Written before the column existed: nothing about the radio, so
            # the site profile's declaration is all a reader has.
            ("legacy_stop", None, None),
        ):
            import_survey_run(
                connection,
                run=_record(run_id, campaign_id=campaign, hardware=hardware),
                observations=[],
                raster_tolerance_hz=6250.0,
            )
    finally:
        connection.close()
    return path


def _digest(database: Path, *args: str) -> str:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--database", str(database), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def test_every_gain_says_whether_it_was_measured_asked_for_or_declared(tmp_path: Path) -> None:
    output = _digest(_database(tmp_path))

    # Three stops, three different gains, so the comparability warning fires.
    assert "IF GAIN VARIES ACROSS STOPS" in output
    assert "25.0 dB IFGR (applied)" in output
    assert "26.0 dB IFGR (requested)" in output
    assert "40.0 dB IFGR (declared)" in output


def test_the_campaign_breakdown_names_unassigned_runs_as_such(tmp_path: Path) -> None:
    output = _digest(_database(tmp_path))

    assert "campaigns" in output
    assert "day1 (2)" in output
    assert "unassigned (1)" in output


def test_a_campaign_filter_narrows_the_digest_to_that_round(tmp_path: Path) -> None:
    output = _digest(_database(tmp_path), "--campaign", "day1")

    assert "campaign day1" in output
    assert "stops total          2" in output
    assert "day1 (2)" in output
    assert "unassigned" not in output
    # The stop that only had a declaration is not in this round at all, so
    # neither is its gain.
    assert "(declared)" not in output


def test_an_unknown_campaign_reports_an_empty_round_rather_than_everything(
    tmp_path: Path,
) -> None:
    output = _digest(_database(tmp_path), "--campaign", "day9")

    assert "stops total          0" in output
    assert "25.0 dB IFGR" not in output
