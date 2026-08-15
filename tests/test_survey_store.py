from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from dmr_iq_surveyor.inventory.store import connect_database, replace_run
from dmr_iq_surveyor.survey.discovery import RfObservation
from dmr_iq_surveyor.survey.profiles import SiteProfile
from dmr_iq_surveyor.survey.store import (
    SurveyRunRecord,
    connect_survey_database,
    fetch_survey_table,
    get_run,
    get_run_observations,
    import_survey_run,
    list_runs,
    upsert_site,
)

SITE = SiteProfile(site_id="home", label="Home")


def _observation(frequency_hz: float, *, persistence: float = 1.0) -> RfObservation:
    return RfObservation(
        measured_center_hz=frequency_hz,
        bandwidth_hz=6000.0,
        peak_dbfs_per_hz=-40.0,
        average_dbfs_per_hz=-50.0,
        noise_floor_dbfs_per_hz=-90.0,
        power_unit="dbfs_per_hz",
        calibrated=False,
        snr_db=30.0,
        p95_snr_db=35.0,
        peak_concentration_db=10.0,
        occupancy_pct=15.0,
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
        raster_error_hz=frequency_hz - round(frequency_hz / 12500.0) * 12500.0,
        spectral_class="narrowband_digital_candidate",
        classification="unknown",
        classification_confidence=0.8,
        classification_method="spectral_only",
        edge_warning=False,
        dc_warning=False,
    )


def _run_record(run_id: str, *, capture_start_utc: str | None, capture_time_source: str) -> SurveyRunRecord:
    return SurveyRunRecord(
        survey_run_id=run_id,
        site_id=SITE.site_id,
        band_profile="test_band",
        source_path=f"/tmp/{run_id}.wav",
        source_sha256=None,
        center_frequency_hz=868_000_000.0,
        sample_rate_hz=200_000.0,
        capture_start_utc=capture_start_utc,
        capture_time_source=capture_time_source,
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


def test_rf_frequencies_has_no_protocol_or_site_or_role_column(tmp_path: Path) -> None:
    connection = connect_survey_database(tmp_path / "db.sqlite3")
    columns = {row[1] for row in connection.execute("PRAGMA table_info(rf_frequencies)")}
    forbidden = {"protocol", "system", "system_id", "site_id", "role", "wacn", "sysid", "nac"}
    assert not (columns & forbidden)
    connection.close()


def test_idempotent_reimport_leaves_row_counts_unchanged(tmp_path: Path) -> None:
    connection = connect_survey_database(tmp_path / "db.sqlite3")
    upsert_site(connection, SITE)
    run = _run_record("r1", capture_start_utc="2026-08-01T00:00:00+00:00", capture_time_source="auxi")
    observations = [_observation(868_050_000.0), _observation(867_930_000.0)]

    import_survey_run(connection, run=run, observations=observations, raster_tolerance_hz=6250.0)
    frequencies_after_first = len(fetch_survey_table(connection, "rf_frequencies"))
    observations_after_first = len(fetch_survey_table(connection, "rf_observations"))

    import_survey_run(connection, run=run, observations=observations, raster_tolerance_hz=6250.0)
    assert len(fetch_survey_table(connection, "rf_frequencies")) == frequencies_after_first
    assert len(fetch_survey_table(connection, "rf_observations")) == observations_after_first
    assert len(fetch_survey_table(connection, "survey_runs")) == 1
    connection.close()


def test_two_runs_accumulate(tmp_path: Path) -> None:
    connection = connect_survey_database(tmp_path / "db.sqlite3")
    upsert_site(connection, SITE)
    run1 = _run_record("r1", capture_start_utc="2026-08-01T00:00:00+00:00", capture_time_source="auxi")
    run2 = _run_record("r2", capture_start_utc="2026-08-10T00:00:00+00:00", capture_time_source="auxi")
    import_survey_run(connection, run=run1, observations=[_observation(868_050_000.0)], raster_tolerance_hz=6250.0)
    import_survey_run(connection, run=run2, observations=[_observation(868_050_000.0)], raster_tolerance_hz=6250.0)

    assert len(fetch_survey_table(connection, "survey_runs")) == 2
    # Same physical frequency in both runs -> one catalog row, two observations.
    assert len(fetch_survey_table(connection, "rf_frequencies")) == 1
    assert len(fetch_survey_table(connection, "rf_observations")) == 2
    connection.close()


def test_first_seen_last_seen_use_capture_time_not_import_order(tmp_path: Path) -> None:
    """Importing an older capture *after* a newer one must still produce
    correct first/last seen -- run ID or import order must never substitute
    for the actual RF capture time."""
    connection = connect_survey_database(tmp_path / "db.sqlite3")
    upsert_site(connection, SITE)
    newer = _run_record("newer", capture_start_utc="2026-08-10T00:00:00+00:00", capture_time_source="auxi")
    older = _run_record("older", capture_start_utc="2026-08-01T00:00:00+00:00", capture_time_source="auxi")

    # Import the newer capture first, then the older one, out of chronological order.
    import_survey_run(connection, run=newer, observations=[_observation(868_050_000.0)], raster_tolerance_hz=6250.0)
    import_survey_run(connection, run=older, observations=[_observation(868_050_000.0)], raster_tolerance_hz=6250.0)

    frequencies = fetch_survey_table(connection, "rf_frequencies")
    assert len(frequencies) == 1
    row = frequencies[0]
    assert row["first_seen_at"] == "2026-08-01T00:00:00+00:00"
    assert row["first_seen_run_id"] == "older"
    assert row["last_seen_at"] == "2026-08-10T00:00:00+00:00"
    assert row["last_seen_run_id"] == "newer"
    connection.close()


def test_undated_run_excluded_from_first_last_seen(tmp_path: Path) -> None:
    connection = connect_survey_database(tmp_path / "db.sqlite3")
    upsert_site(connection, SITE)
    dated = _run_record("dated", capture_start_utc="2026-08-01T00:00:00+00:00", capture_time_source="auxi")
    undated = _run_record("undated", capture_start_utc=None, capture_time_source="unknown")

    import_survey_run(connection, run=dated, observations=[_observation(868_050_000.0)], raster_tolerance_hz=6250.0)
    import_survey_run(connection, run=undated, observations=[_observation(868_050_000.0)], raster_tolerance_hz=6250.0)

    row = fetch_survey_table(connection, "rf_frequencies")[0]
    assert row["first_seen_at"] == "2026-08-01T00:00:00+00:00"
    assert row["last_seen_at"] == "2026-08-01T00:00:00+00:00"
    assert row["observation_count"] == 2
    assert row["undated_observation_count"] == 1
    connection.close()


def test_deleting_a_run_recomputes_timestamps_rather_than_leaving_stale_values(tmp_path: Path) -> None:
    connection = connect_survey_database(tmp_path / "db.sqlite3")
    upsert_site(connection, SITE)
    early = _run_record("early", capture_start_utc="2026-08-01T00:00:00+00:00", capture_time_source="auxi")
    late = _run_record("late", capture_start_utc="2026-08-10T00:00:00+00:00", capture_time_source="auxi")
    import_survey_run(connection, run=early, observations=[_observation(868_050_000.0)], raster_tolerance_hz=6250.0)
    import_survey_run(connection, run=late, observations=[_observation(868_050_000.0)], raster_tolerance_hz=6250.0)

    # Re-importing "early" with zero observations removes its rows for that
    # frequency; first_seen_at must move forward to "late", not stay stale.
    empty_early = _run_record("early", capture_start_utc="2026-08-01T00:00:00+00:00", capture_time_source="auxi")
    import_survey_run(connection, run=empty_early, observations=[], raster_tolerance_hz=6250.0)

    row = fetch_survey_table(connection, "rf_frequencies")[0]
    assert row["first_seen_at"] == "2026-08-10T00:00:00+00:00"
    assert row["first_seen_run_id"] == "late"
    connection.close()


def test_existing_dmr_database_opens_and_extends_cleanly(tmp_path: Path) -> None:
    """A database built entirely by the pre-Phase-6 DMR code must open,
    upgrade in place, and keep serving the old tables unchanged."""
    db_path = tmp_path / "db.sqlite3"
    old_connection = connect_database(db_path)
    replace_run(
        old_connection,
        run_id="dmr_run",
        source_dir="/tmp/dmr",
        attempts=[
            {
                "attempt_key": "a1",
                "candidate_id": "C0001",
                "recording_id": "rec1",
                "frequency_hz": 164_537_500.0,
                "iq_order": "IQ",
                "status": "dmr_confirmed_clean",
                "talkgroup_ids": [],
                "radio_ids": [],
                "output_dir": "/tmp/dmr/C0001",
            }
        ],
    )
    old_connection.close()

    survey_connection = connect_survey_database(db_path)
    # Old DMR tables and data survive untouched.
    old_channels = [dict(row) for row in survey_connection.execute("SELECT * FROM channels")]
    assert len(old_channels) == 1
    assert old_channels[0]["frequency_hz"] == 164_537_500.0
    # New survey tables now exist alongside them.
    upsert_site(survey_connection, SITE)
    run = _run_record("r1", capture_start_utc="2026-08-01T00:00:00+00:00", capture_time_source="auxi")
    import_survey_run(
        survey_connection, run=run, observations=[_observation(868_050_000.0)], raster_tolerance_hz=6250.0
    )
    assert len(fetch_survey_table(survey_connection, "rf_observations")) == 1
    survey_connection.close()


def test_get_run_and_get_run_observations(tmp_path: Path) -> None:
    connection = connect_survey_database(tmp_path / "db.sqlite3")
    upsert_site(connection, SITE)
    run = _run_record("r1", capture_start_utc="2026-08-01T00:00:00+00:00", capture_time_source="auxi")
    import_survey_run(connection, run=run, observations=[_observation(868_050_000.0)], raster_tolerance_hz=6250.0)

    assert get_run(connection, "r1") is not None
    assert get_run(connection, "missing") is None
    observations = get_run_observations(connection, "r1")
    assert len(observations) == 1
    assert observations[0]["nominal_frequency_hz"] == 868_050_000.0
    connection.close()


def test_list_runs_filters_by_site(tmp_path: Path) -> None:
    connection = connect_survey_database(tmp_path / "db.sqlite3")
    upsert_site(connection, SITE)
    upsert_site(connection, SiteProfile(site_id="away", label="Away"))
    run_home = _run_record("home_run", capture_start_utc="2026-08-01T00:00:00+00:00", capture_time_source="auxi")
    run_away = replace(run_home, survey_run_id="away_run", site_id="away")
    import_survey_run(connection, run=run_home, observations=[], raster_tolerance_hz=6250.0)
    import_survey_run(connection, run=run_away, observations=[], raster_tolerance_hz=6250.0)

    assert {row["survey_run_id"] for row in list_runs(connection)} == {"home_run", "away_run"}
    assert {row["survey_run_id"] for row in list_runs(connection, site_id="home")} == {"home_run"}
    connection.close()
