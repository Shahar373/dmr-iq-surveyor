"""End-to-end Phase 7: registry -> survey runs -> measurements -> solutions."""

from __future__ import annotations

import json
from pathlib import Path

from fixtures.geo_scenario import (
    SITE_CSV,
    Transmitter,
    build_database,
    fast_solve_settings,
    seed_run,
)

from dmr_iq_surveyor.geo.model import haversine_m
from dmr_iq_surveyor.geo.pipeline import (
    STATUS_FREQUENCY_UNKNOWN,
    STATUS_NO_MEASUREMENTS,
    build_map_geojson,
    import_reference_sites,
    materialise_measurements,
    site_overview,
    solve_all_sites,
)
from dmr_iq_surveyor.geo.store import connect_geo_database, solution_history

SITE30 = Transmitter(867_762_500.0, 32.050, 34.800, reference_level_db=25.0, path_loss_exponent=3.4)
SITE33 = Transmitter(866_712_500.0, 32.120, 34.870, reference_level_db=25.0, path_loss_exponent=3.4)
SHARED = Transmitter(867_912_500.0, 32.050, 34.800, reference_level_db=25.0)

STOPS = [
    (32.045, 34.795), (32.056, 34.806), (32.041, 34.809), (32.059, 34.791),
    (32.020, 34.760), (32.085, 34.770), (32.075, 34.855), (32.015, 34.850),
    (32.115, 34.865), (32.126, 34.876), (32.110, 34.880),
    (31.950, 34.700), (32.200, 34.700), (32.200, 34.950), (31.950, 34.950),
]


def _seed(database: Path, stops: list[tuple[float, float]], *, prefix: str = "run") -> None:
    connection = build_database(database)
    for index, (latitude, longitude) in enumerate(stops):
        seed_run(
            connection,
            run_id=f"{prefix}_{index:02d}",
            latitude=latitude,
            longitude=longitude,
            transmitters=[SITE30, SITE33, SHARED],
            capture_start_utc=f"2026-08-{1 + index // 12:02d}T{6 + index % 12:02d}:00:00+00:00",
        )
    connection.close()


def test_full_chain_produces_a_region_for_a_well_observed_site(tmp_path: Path) -> None:
    database = tmp_path / "db.sqlite3"
    _seed(database, STOPS)

    measurements = materialise_measurements(database_path=database)
    assert measurements["run_count"] == len(STOPS)
    assert measurements["summary"]["detections"] > 0
    assert measurements["summary"]["non_detections"] > 0
    # Both sites on the reused frequency are excluded, every run.
    assert measurements["summary"]["ambiguous"] == 2 * len(STOPS)

    report = solve_all_sites(
        database_path=database, output_root=tmp_path / "out", settings=fast_solve_settings()
    )
    solutions = {row["site_key"]: row for row in report["solutions"]}

    site30 = solutions["BEE00:37D:1:30"]
    assert site30["status"] == "ok"
    assert haversine_m(
        site30["mode_latitude"], site30["mode_longitude"], SITE30.latitude, SITE30.longitude
    ) < 2000.0
    assert site30["area_km2_90"] > 0

    assert solutions["BEE00:37D:1:81"]["status"] == STATUS_FREQUENCY_UNKNOWN
    for key in ("BEE00:37D:1:50", "BEE00:37D:1:82"):
        assert solutions[key]["status"] == STATUS_NO_MEASUREMENTS
        assert solutions[key]["excluded_count"] == len(STOPS)


def test_reports_and_geojson_are_written(tmp_path: Path) -> None:
    database = tmp_path / "db.sqlite3"
    _seed(database, STOPS)
    materialise_measurements(database_path=database)
    report = solve_all_sites(
        database_path=database,
        output_root=tmp_path / "out",
        solve_batch_id="batch_a",
        settings=fast_solve_settings(),
    )
    reports = tmp_path / "out" / "reports"
    assert (reports / "geolocation_batch_a.json").is_file()
    assert (reports / "geolocation_batch_a.geojson").is_file()
    markdown = (reports / "geolocation_batch_a.md").read_text(encoding="utf-8")
    assert "not a tower coordinate" in markdown
    assert "BEE00:37D:1:30" in markdown
    assert "simulcast" in markdown
    assert report["source_model"] == "single_transmitter_assumed"


def test_map_geojson_carries_evidence_alongside_regions(tmp_path: Path) -> None:
    database = tmp_path / "db.sqlite3"
    _seed(database, STOPS)
    materialise_measurements(database_path=database)
    solve_all_sites(
        database_path=database, output_root=tmp_path / "out", settings=fast_solve_settings()
    )
    collection = build_map_geojson(database_path=database)
    kinds = [feature["properties"]["kind"] for feature in collection["features"]]
    assert "measurement" in kinds
    assert "credible_region" in kinds
    assert "estimate" in kinds
    measurement = next(
        feature for feature in collection["features"]
        if feature["properties"]["kind"] == "measurement"
    )
    assert "attribution" in measurement["properties"]
    assert "usability" in measurement["properties"]


def test_solutions_accumulate_as_history(tmp_path: Path) -> None:
    database = tmp_path / "db.sqlite3"
    _seed(database, STOPS)
    materialise_measurements(database_path=database)
    settings = fast_solve_settings()
    solve_all_sites(database_path=database, solve_batch_id="first", settings=settings)
    solve_all_sites(database_path=database, solve_batch_id="second", settings=settings)
    solve_all_sites(database_path=database, solve_batch_id="second", settings=settings)

    connection = connect_geo_database(database)
    site_id = connection.execute(
        "SELECT p25_site_id FROM p25_sites WHERE site_key = 'BEE00:37D:1:30'"
    ).fetchone()["p25_site_id"]
    history = solution_history(connection, int(site_id))
    connection.close()
    assert [row["solve_batch_id"] for row in history] == ["first", "second"]


def test_overview_lists_every_site_including_unmeasurable_ones(tmp_path: Path) -> None:
    database = tmp_path / "db.sqlite3"
    _seed(database, STOPS[:4])
    materialise_measurements(database_path=database)
    solve_all_sites(database_path=database, settings=fast_solve_settings())
    overview = {row["site_key"]: row for row in site_overview(database_path=database)}
    assert len(overview) == 5
    assert overview["BEE00:37D:1:81"]["channels"] == []
    assert overview["BEE00:37D:1:50"]["channels"][0]["sharing_site_count"] == 2
    assert overview["BEE00:37D:1:30"]["detections"] > 0


def test_importing_the_snapshot_from_a_file_matches_the_inline_fixture(tmp_path: Path) -> None:
    csv_path = tmp_path / "sites.csv"
    csv_path.write_text(SITE_CSV, encoding="utf-8")
    summary = import_reference_sites(
        csv_path, database_path=tmp_path / "db.sqlite3", snapshot_id="snap"
    )
    assert summary["sites_created"] == 5
    assert summary["channels_imported"] == 4
    assert summary["sites_without_frequency"] == 1


def test_solving_a_single_site_leaves_the_others_alone(tmp_path: Path) -> None:
    database = tmp_path / "db.sqlite3"
    _seed(database, STOPS)
    materialise_measurements(database_path=database)
    report = solve_all_sites(
        database_path=database,
        site_keys=["BEE00:37D:1:30"],
        settings=fast_solve_settings(),
    )
    assert [row["site_key"] for row in report["solutions"]] == ["BEE00:37D:1:30"]


def test_stored_solution_geojson_round_trips(tmp_path: Path) -> None:
    database = tmp_path / "db.sqlite3"
    _seed(database, STOPS)
    materialise_measurements(database_path=database)
    solve_all_sites(database_path=database, settings=fast_solve_settings())
    connection = connect_geo_database(database)
    row = connection.execute(
        "SELECT geojson FROM geo_solutions WHERE p25_site_id = "
        "(SELECT p25_site_id FROM p25_sites WHERE site_key = 'BEE00:37D:1:30')"
    ).fetchone()
    connection.close()
    payload = json.loads(row["geojson"])
    assert payload["type"] == "FeatureCollection"
    assert payload["features"]


def test_latest_solution_survives_a_clock_that_jumps_backwards(tmp_path: Path) -> None:
    """A Raspberry Pi has no RTC: it boots stale and jumps when NTP arrives.

    A solve run later in the day can therefore carry an earlier timestamp
    than one run before it, and ranking on that string showed a superseded
    region on the map.
    """
    from dmr_iq_surveyor.geo.store import latest_solutions, store_solution

    database = tmp_path / "db.sqlite3"
    connection = build_database(database)
    site_id = int(
        connection.execute(
            "SELECT p25_site_id FROM p25_sites WHERE site_key = 'BEE00:37D:1:30'"
        ).fetchone()["p25_site_id"]
    )
    base = {
        "p25_site_id": site_id,
        "method": "m",
        "source_model": "s",
        "status": "ok",
        "detection_count": 3,
        "non_detection_count": 1,
        "excluded_count": 0,
        "level_metric": "snr_db",
        "tool_version": "test",
    }
    store_solution(
        connection,
        solve_batch_id="earlier_run",
        row={**base, "solved_at": "2026-09-02T10:00:00+00:00", "area_km2_90": 100.0},
    )
    store_solution(
        connection,
        solve_batch_id="later_run_stale_clock",
        row={**base, "solved_at": "2025-01-01T00:00:00+00:00", "area_km2_90": 5.0},
    )
    latest = [row for row in latest_solutions(connection) if row["site_key"] == "BEE00:37D:1:30"]
    assert len(latest) == 1
    assert latest[0]["solve_batch_id"] == "later_run_stale_clock"

    # Two solves inside the same second must not duplicate the site either.
    store_solution(
        connection,
        solve_batch_id="same_second",
        row={**base, "solved_at": "2026-09-02T10:00:00+00:00", "area_km2_90": 7.0},
    )
    latest = [row for row in latest_solutions(connection) if row["site_key"] == "BEE00:37D:1:30"]
    assert len(latest) == 1
    assert latest[0]["solve_batch_id"] == "same_second"
    connection.close()


def test_reimporting_a_corrected_snapshot_rebuilds_stale_measurements(tmp_path: Path) -> None:
    """A resolved ambiguity must actually take effect.

    Measurements are derived from the frequency map at the time. Leaving them
    behind would mean a corrected snapshot silently never applied, while the
    reports went on asserting the old verdict.
    """
    from dmr_iq_surveyor.geo.store import connect_geo_database, fetch_all_measurements

    database = tmp_path / "db.sqlite3"
    _seed(database, STOPS[:4])
    first = materialise_measurements(database_path=database)
    assert first["summary"]["ambiguous"] > 0, "site 50 and 82 share a frequency"

    corrected = tmp_path / "corrected.csv"
    corrected.write_text(
        SITE_CSV.replace(
            "BEE00,37D,1,82,NEIGHBOR_ONLY,867.912500,,reuse with site 50",
            "BEE00,37D,1,82,NEIGHBOR_ONLY,868.437500,,ambiguity resolved",
        ),
        encoding="utf-8",
    )
    summary = import_reference_sites(
        corrected, database_path=database, snapshot_id="test_snapshot"
    )
    assert "measurements_rebuilt" in summary
    assert any("rebuilt against the new snapshot" in w for w in summary["warnings"])
    assert summary["measurements_rebuilt"]["ambiguous"] == 0, (
        "the frequency is no longer shared, so nothing should still be excluded for it"
    )

    connection = connect_geo_database(database)
    frequencies = {
        row["frequency_hz"] for row in fetch_all_measurements(connection) if row["site_key"].endswith(":82")
    }
    connection.close()
    assert frequencies == {868_437_500.0}


def test_a_solution_reports_the_flags_its_evidence_carries(tmp_path: Path) -> None:
    database = tmp_path / "db.sqlite3"
    connection = build_database(database)
    for index, (latitude, longitude) in enumerate(STOPS[:5]):
        seed_run(
            connection,
            run_id=f"run_{index:02d}",
            latitude=latitude,
            longitude=longitude,
            transmitters=[SITE30],
            site_id=f"stop_{index}",
            gain=40.0 if index else 31.0,
            capture_start_utc=f"2026-08-01T{8 + index:02d}:00:00+00:00",
        )
    connection.close()

    materialise_measurements(database_path=database)
    report = solve_all_sites(database_path=database, settings=fast_solve_settings())
    solution = next(row for row in report["solutions"] if row["site_key"] == "BEE00:37D:1:30")
    assert any("gain_differs_from_campaign" in warning for warning in solution.get("warnings", []))


def test_an_exclusion_written_after_the_measurements_is_honoured_by_the_solve(tmp_path: Path) -> None:
    """The failure this guards against: a drive bin superseded by a later
    day's pass is excluded AFTER its measurements were built, and the solver
    reads the measurements. Without a refresh both days count -- the double
    evidence the supersede exists to prevent."""
    from dmr_iq_surveyor.geo.pipeline import runs_with_stale_exclusions
    from dmr_iq_surveyor.geo.store import clear_run_exclusion, exclude_run

    database = tmp_path / "stale.sqlite3"
    _seed(database, STOPS)
    materialise_measurements(database_path=database)

    connection = connect_geo_database(database)
    try:
        assert runs_with_stale_exclusions(connection) == []
        exclude_run(connection, "run_00", "superseded by run_00_day2", scope="all")
        assert runs_with_stale_exclusions(connection) == ["run_00"]
    finally:
        connection.close()

    report = solve_all_sites(database_path=database, settings=fast_solve_settings())
    assert report["measurements_refreshed"] == ["run_00"]
    connection = connect_geo_database(database)
    try:
        assert runs_with_stale_exclusions(connection) == []
        usable = connection.execute(
            "SELECT COUNT(*) AS n FROM geo_measurements "
            "WHERE survey_run_id = 'run_00' AND usability = 'usable'"
        ).fetchone()["n"]
        assert usable == 0
        for row in report["solutions"]:
            assert "run_00" not in row["input_run_ids"]

        # Lifting it is the same staleness the other way round.
        clear_run_exclusion(connection, "run_00")
        assert runs_with_stale_exclusions(connection) == ["run_00"]
    finally:
        connection.close()
    report = solve_all_sites(database_path=database, settings=fast_solve_settings())
    assert report["measurements_refreshed"] == ["run_00"]
    assert any("run_00" in row["input_run_ids"] for row in report["solutions"])

    # A campaign that is already consistent refreshes nothing.
    report = solve_all_sites(database_path=database, settings=fast_solve_settings())
    assert report["measurements_refreshed"] == []
    assert report["measurements_refreshed_summary"] is None
