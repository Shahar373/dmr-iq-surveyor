from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from dmr_iq_surveyor.survey.compare import (
    STATUS_MISSING_THIS_RUN,
    STATUS_NEW,
    STATUS_NOT_COMPARABLE,
    STATUS_OCCUPANCY_CHANGE,
    STATUS_PERSISTENCE_CHANGE,
    STATUS_SNR_CHANGE,
    STATUS_STABLE,
    compare_runs,
)
from dmr_iq_surveyor.survey.discovery import RfObservation
from dmr_iq_surveyor.survey.profiles import ComparisonTolerances, SiteProfile
from dmr_iq_surveyor.survey.store import (
    SurveyRunRecord,
    connect_survey_database,
    import_survey_run,
    upsert_site,
)

SITE = SiteProfile(site_id="home", label="Home")
TOLERANCES = ComparisonTolerances(
    snr_delta_db=3.0, occupancy_delta_pct=10.0, persistence_delta=0.25
)


def _observation(
    frequency_hz: float,
    *,
    snr_db: float = 30.0,
    occupancy_pct: float = 15.0,
    persistence: float = 1.0,
) -> RfObservation:
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
        occupancy_pct=occupancy_pct,
        occupancy_threshold_db=8.0,
        occupancy_sample_count=1000,
        persistence=persistence,
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


def _base_run(run_id: str) -> SurveyRunRecord:
    return SurveyRunRecord(
        survey_run_id=run_id,
        site_id=SITE.site_id,
        band_profile="test_band",
        source_path=f"/tmp/{run_id}.wav",
        source_sha256=None,
        center_frequency_hz=868_000_000.0,
        sample_rate_hz=200_000.0,
        capture_start_utc="2026-08-01T00:00:00+00:00",
        capture_time_source="auxi",
        requested_start_hz=867_800_000.0,
        requested_stop_hz=868_200_000.0,
        usable_low_hz=867_900_000.0,
        usable_high_hz=868_100_000.0,
        coverage_status="partial",
        duration_seconds=6.0,
        analyzed_seconds=6.0,
        segment_count=6,
        occupancy_threshold_db=8.0,
        detection_settings={"scan_step_hz": 6250.0},
        tool_version="0.9.0",
    )


def _setup(tmp_path: Path):
    connection = connect_survey_database(tmp_path / "db.sqlite3")
    upsert_site(connection, SITE)
    return connection


def test_new_missing_and_stable(tmp_path: Path) -> None:
    connection = _setup(tmp_path)
    baseline = _base_run("baseline")
    target = _base_run("target")

    import_survey_run(
        connection,
        run=baseline,
        observations=[_observation(868_000_000.0), _observation(867_950_000.0)],
        raster_tolerance_hz=6250.0,
    )
    import_survey_run(
        connection,
        run=target,
        observations=[_observation(868_000_000.0), _observation(868_012_500.0)],
        raster_tolerance_hz=6250.0,
    )
    rows = compare_runs(
        connection, baseline_run_id="baseline", target_run_id="target", tolerances=TOLERANCES
    )
    statuses = {row.nominal_frequency_hz: row.status for row in rows}
    assert statuses[868_000_000.0] == STATUS_STABLE
    assert statuses[867_950_000.0] == STATUS_MISSING_THIS_RUN
    assert statuses[868_012_500.0] == STATUS_NEW
    connection.close()


def test_snr_occupancy_persistence_changes(tmp_path: Path) -> None:
    connection = _setup(tmp_path)
    baseline = _base_run("baseline")
    target = _base_run("target")

    import_survey_run(
        connection,
        run=baseline,
        observations=[
            _observation(868_000_000.0, snr_db=10.0),
            _observation(868_012_500.0, occupancy_pct=10.0),
            _observation(868_025_000.0, persistence=1.0),
        ],
        raster_tolerance_hz=6250.0,
    )
    import_survey_run(
        connection,
        run=target,
        observations=[
            _observation(868_000_000.0, snr_db=25.0),  # +15 dB -> SNR_CHANGE
            _observation(868_012_500.0, occupancy_pct=40.0),  # +30 pts -> OCCUPANCY_CHANGE
            _observation(868_025_000.0, persistence=0.2),  # -0.8 -> PERSISTENCE_CHANGE
        ],
        raster_tolerance_hz=6250.0,
    )
    rows = compare_runs(
        connection, baseline_run_id="baseline", target_run_id="target", tolerances=TOLERANCES
    )
    statuses = {row.nominal_frequency_hz: row.status for row in rows}
    assert statuses[868_000_000.0] == STATUS_SNR_CHANGE
    assert statuses[868_012_500.0] == STATUS_OCCUPANCY_CHANGE
    assert statuses[868_025_000.0] == STATUS_PERSISTENCE_CHANGE
    connection.close()


def test_not_comparable_different_sites(tmp_path: Path) -> None:
    connection = _setup(tmp_path)
    upsert_site(connection, SiteProfile(site_id="away", label="Away"))
    baseline = _base_run("baseline")
    target = replace(_base_run("target"), site_id="away")

    import_survey_run(connection, run=baseline, observations=[_observation(868_000_000.0)], raster_tolerance_hz=6250.0)
    import_survey_run(connection, run=target, observations=[_observation(868_000_000.0)], raster_tolerance_hz=6250.0)

    rows = compare_runs(
        connection, baseline_run_id="baseline", target_run_id="target", tolerances=TOLERANCES
    )
    assert len(rows) == 1
    assert rows[0].status == STATUS_NOT_COMPARABLE
    assert not rows[0].comparable
    assert "site" in rows[0].reason
    connection.close()


def test_not_comparable_different_occupancy_threshold(tmp_path: Path) -> None:
    connection = _setup(tmp_path)
    baseline = _base_run("baseline")
    target = replace(_base_run("target"), occupancy_threshold_db=12.0)

    import_survey_run(connection, run=baseline, observations=[_observation(868_000_000.0)], raster_tolerance_hz=6250.0)
    import_survey_run(connection, run=target, observations=[_observation(868_000_000.0)], raster_tolerance_hz=6250.0)

    rows = compare_runs(
        connection, baseline_run_id="baseline", target_run_id="target", tolerances=TOLERANCES
    )
    assert len(rows) == 1
    assert rows[0].status == STATUS_NOT_COMPARABLE
    assert "occupancy_threshold_db" in rows[0].reason
    connection.close()


def test_frequency_outside_usable_passband_is_not_comparable_not_missing(tmp_path: Path) -> None:
    connection = _setup(tmp_path)
    baseline = _base_run("baseline")
    # Target's usable passband does not cover 868.05 MHz.
    target = replace(_base_run("target"), usable_low_hz=868_060_000.0, usable_high_hz=868_100_000.0)

    import_survey_run(
        connection, run=baseline, observations=[_observation(868_050_000.0)], raster_tolerance_hz=6250.0
    )
    import_survey_run(connection, run=target, observations=[], raster_tolerance_hz=6250.0)

    rows = compare_runs(
        connection, baseline_run_id="baseline", target_run_id="target", tolerances=TOLERANCES
    )
    assert len(rows) == 1
    assert rows[0].status == STATUS_NOT_COMPARABLE
    assert rows[0].status != STATUS_MISSING_THIS_RUN
    assert "passband" in rows[0].reason
    connection.close()


def test_not_comparable_analyzed_seconds_ratio_exceeded(tmp_path: Path) -> None:
    connection = _setup(tmp_path)
    baseline = _base_run("baseline")
    target = replace(_base_run("target"), analyzed_seconds=100.0)  # baseline is 6s -> ratio > 4x

    import_survey_run(connection, run=baseline, observations=[_observation(868_000_000.0)], raster_tolerance_hz=6250.0)
    import_survey_run(connection, run=target, observations=[_observation(868_000_000.0)], raster_tolerance_hz=6250.0)

    rows = compare_runs(
        connection,
        baseline_run_id="baseline",
        target_run_id="target",
        tolerances=ComparisonTolerances(analyzed_seconds_ratio_limit=4.0),
    )
    assert len(rows) == 1
    assert rows[0].status == STATUS_NOT_COMPARABLE
    assert "analyzed_seconds" in rows[0].reason
    connection.close()
