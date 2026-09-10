"""`survey compare` across two collection rounds.

Comparing two rounds of the same place is the point of running a second one,
so this must never block. What it must do is say which differences might be
the rounds rather than the RF: each round establishes its own reference gain
and noise floor, so a level delta between them is not the same evidence as a
level delta within one.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from typer.testing import CliRunner

from dmr_iq_surveyor.cli_app import app
from dmr_iq_surveyor.reporting.json_report import build_comparison_report
from dmr_iq_surveyor.reporting.markdown import render_comparison_markdown
from dmr_iq_surveyor.survey.discovery import RfObservation
from dmr_iq_surveyor.survey.pipeline import run_comparison
from dmr_iq_surveyor.survey.profiles import SiteProfile
from dmr_iq_surveyor.survey.store import (
    SurveyRunRecord,
    connect_survey_database,
    import_survey_run,
    upsert_site,
)

runner = CliRunner()

SITE = SiteProfile(
    site_id="mobile",
    label="Mobile",
    latitude=32.05,
    longitude=34.79,
    gain_mode="manual",
    gain=40.0,
    lna_state=2,
)


def _observation(frequency_hz: float, *, snr_db: float = 30.0) -> RfObservation:
    return RfObservation(
        measured_center_hz=frequency_hz,
        bandwidth_hz=6000.0,
        peak_dbfs_per_hz=-40.0,
        average_dbfs_per_hz=-50.0,
        noise_floor_dbfs_per_hz=-90.0,
        power_unit="dbfs_per_hz",
        calibrated=False,
        snr_db=snr_db,
        p95_snr_db=snr_db + 3.0,
        peak_concentration_db=10.0,
        occupancy_pct=15.0,
        occupancy_threshold_db=8.0,
        occupancy_sample_count=1000,
        persistence=1.0,
        segments_detected=5,
        segments_analyzed=5,
        equivalent_width_hz=3000.0,
        spectral_fill=0.5,
        symmetry=0.9,
        nearest_raster_hz=round(frequency_hz / 12500.0) * 12500.0,
        raster_spacing_hz=12500.0,
        raster_error_hz=0.0,
        spectral_class="narrowband_digital_candidate",
        classification="unknown",
        classification_confidence=0.8,
        classification_method="spectral_only",
        edge_warning=False,
        dc_warning=False,
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


def _database(tmp_path: Path, *, target_campaign: str | None) -> Path:
    path = tmp_path / "compare.sqlite3"
    connection = connect_survey_database(path)
    try:
        upsert_site(connection, SITE)
        for run_id, campaign in (("baseline", "day1"), ("target", target_campaign)):
            import_survey_run(
                connection,
                run=_record(run_id, campaign_id=campaign),
                observations=[_observation(867_762_500.0)],
                raster_tolerance_hz=6250.0,
            )
    finally:
        connection.close()
    return path


# -- the report --------------------------------------------------------------


def test_two_rounds_are_flagged_but_never_blocked(tmp_path: Path) -> None:
    """The contract in one test: `campaign_differs` is true, a warning is
    carried, and every frequency still compares normally."""
    path = _database(tmp_path, target_campaign="day2")

    report = run_comparison(
        tmp_path / "out",
        baseline_run_id="baseline",
        target_run_id="target",
        database_path=path,
    )

    assert report["campaign_differs"] is True
    assert report["baseline_campaign_id"] == "day1"
    assert report["target_campaign_id"] == "day2"
    assert any("campaign_differs" in warning for warning in report["warnings"])
    # Not blocked: the comparison happened, and nothing was marked
    # incomparable because of the campaigns.
    assert report["status_counts"].get("NOT_COMPARABLE", 0) == 0
    assert report["status_counts"].get("STABLE", 0) >= 1


def test_one_round_compared_with_itself_carries_no_warning(tmp_path: Path) -> None:
    path = _database(tmp_path, target_campaign="day1")

    report = run_comparison(
        tmp_path / "out",
        baseline_run_id="baseline",
        target_run_id="target",
        database_path=path,
    )

    assert report["campaign_differs"] is False
    assert report["warnings"] == []
    assert report["baseline_campaign_id"] == report["target_campaign_id"] == "day1"


def test_an_unassigned_run_differs_from_an_assigned_one(tmp_path: Path) -> None:
    """`None` is a campaign answer, not a missing one: a run recorded before
    campaigns existed was not taken under this round."""
    path = _database(tmp_path, target_campaign=None)

    report = run_comparison(
        tmp_path / "out",
        baseline_run_id="baseline",
        target_run_id="target",
        database_path=path,
    )

    assert report["campaign_differs"] is True
    assert report["target_campaign_id"] is None
    assert any("unassigned" in warning for warning in report["warnings"])


def test_both_campaign_ids_are_reported_even_when_they_agree() -> None:
    """A reader must not have to infer from the absence of a warning that
    both runs were assigned at all."""
    report = build_comparison_report(
        baseline_run_id="a",
        target_run_id="b",
        rows=[],
        baseline_campaign_id=None,
        target_campaign_id=None,
    )

    assert report["campaign_differs"] is False
    assert "baseline_campaign_id" in report
    assert "target_campaign_id" in report


def test_the_report_stays_serialisable(tmp_path: Path) -> None:
    path = _database(tmp_path, target_campaign="day2")

    run_comparison(
        tmp_path / "out",
        baseline_run_id="baseline",
        target_run_id="target",
        database_path=path,
    )

    written = json.loads(
        (tmp_path / "out" / "reports" / "comparison_baseline_target.json").read_text(
            encoding="utf-8"
        )
    )
    assert written["campaign_differs"] is True


# -- the rendered forms ------------------------------------------------------


def test_the_markdown_names_both_rounds_and_warns_once() -> None:
    rendered = render_comparison_markdown(
        baseline_run_id="a",
        target_run_id="b",
        rows=[],
        baseline_campaign_id="day1",
        target_campaign_id="day2",
    )

    assert "campaign day1" in rendered
    assert "campaign day2" in rendered
    assert rendered.count("**campaign_differs**") == 1


def test_the_markdown_says_unassigned_rather_than_leaving_it_blank() -> None:
    rendered = render_comparison_markdown(
        baseline_run_id="a", target_run_id="b", rows=[], baseline_campaign_id="day1"
    )

    assert "campaign unassigned" in rendered


def test_the_markdown_is_unchanged_when_the_rounds_agree() -> None:
    rendered = render_comparison_markdown(
        baseline_run_id="a",
        target_run_id="b",
        rows=[],
        baseline_campaign_id="day1",
        target_campaign_id="day1",
    )

    assert "campaign_differs" not in rendered


def test_the_cli_prints_the_warning_without_failing(tmp_path: Path) -> None:
    path = _database(tmp_path, target_campaign="day2")

    result = runner.invoke(
        app,
        [
            "survey", "compare", "baseline", "target",
            "--database", str(path),
            "--output", str(tmp_path / "out"),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "campaign_differs" in result.output.replace("\n", "")


def test_a_genuinely_incomparable_pair_is_still_incomparable(tmp_path: Path) -> None:
    """The campaign warning must not become a substitute for the checks that
    really do block: different sites are still `NOT_COMPARABLE`."""
    path = tmp_path / "sites.sqlite3"
    connection = connect_survey_database(path)
    try:
        upsert_site(connection, SITE)
        upsert_site(connection, replace(SITE, site_id="other", label="Other"))
        import_survey_run(
            connection,
            run=_record("baseline", campaign_id="day1"),
            observations=[_observation(867_762_500.0)],
            raster_tolerance_hz=6250.0,
        )
        import_survey_run(
            connection,
            run=_record("target", campaign_id="day2", site_id="other"),
            observations=[_observation(867_762_500.0)],
            raster_tolerance_hz=6250.0,
        )
    finally:
        connection.close()

    report = run_comparison(
        tmp_path / "out",
        baseline_run_id="baseline",
        target_run_id="target",
        database_path=path,
    )

    assert report["campaign_differs"] is True
    assert report["status_counts"].get("NOT_COMPARABLE", 0) == 1
