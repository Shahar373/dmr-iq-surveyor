"""Phase 8 Stage 1: fixed-frequency emitter targets, end to end."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fixtures.geo_scenario import Transmitter, build_database, fast_solve_settings, seed_run
from typer.testing import CliRunner

from dmr_iq_surveyor.cli_app import app
from dmr_iq_surveyor.geo.emitters import (
    ATTRIBUTION_AMBIGUOUS_NEIGHBOUR,
    ATTRIBUTION_FREQUENCY_MATCH,
    EMISSION_INTERMITTENT,
    STATUS_NO_MEASUREMENTS,
    USABILITY_ABSENCE_NOT_EVIDENCE,
    EmitterError,
    build_emitter_run_measurements,
    connect_emitter_database,
    latest_emitter_solutions,
    list_emitters,
    materialise_emitter_measurements,
    remove_emitter,
    solve_emitters,
    upsert_emitter,
)
from dmr_iq_surveyor.geo.measurements import (
    USABILITY_AMBIGUOUS,
    USABILITY_NOT_COVERED,
    USABILITY_USABLE,
)
from dmr_iq_surveyor.geo.model import haversine_m
from dmr_iq_surveyor.geo.store import connect_geo_database

runner = CliRunner()

NEAR = Transmitter(867_762_500.0, 32.050, 34.800, reference_level_db=25.0, path_loss_exponent=3.4)
FAR = Transmitter(866_712_500.0, 32.600, 35.300, reference_level_db=25.0)

STOPS = [
    (32.045, 34.795), (32.056, 34.806), (32.041, 34.809), (32.059, 34.791),
    (32.020, 34.760), (32.085, 34.770), (32.075, 34.855), (32.015, 34.850),
    (31.950, 34.700), (32.200, 34.700), (32.200, 34.950), (31.950, 34.950),
]


def _seed(database: Path, stops: list[tuple[float, float]] = STOPS) -> None:
    connection = build_database(database)
    for index, (latitude, longitude) in enumerate(stops):
        seed_run(
            connection,
            run_id=f"run_{index:02d}",
            latitude=latitude,
            longitude=longitude,
            transmitters=[NEAR, FAR],
            capture_start_utc=f"2026-08-01T{6 + index:02d}:00:00+00:00",
        )
    connection.close()


def _declare(database: Path, **overrides) -> None:
    connection = connect_emitter_database(database)
    arguments = {"emitter_key": "near", "frequency_hz": NEAR.frequency_hz, "label": "test"}
    arguments.update(overrides)
    upsert_emitter(connection, **arguments)
    connection.close()


def test_an_existing_database_upgrades_in_place_and_p25_tables_are_untouched(
    tmp_path: Path,
) -> None:
    database = tmp_path / "db.sqlite3"
    _seed(database)
    before = connect_geo_database(database)
    tables_before = {
        row[0] for row in before.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    schema_before = {
        name: [tuple(column) for column in before.execute(f"PRAGMA table_info({name})")]
        for name in tables_before
    }
    before.close()

    _declare(database)
    solve_emitters(database_path=database, settings=fast_solve_settings())

    after = connect_emitter_database(database)
    for name, columns in schema_before.items():
        assert [tuple(column) for column in after.execute(f"PRAGMA table_info({name})")] == columns
    assert after.execute("SELECT COUNT(*) FROM geo_measurements").fetchone()[0] == 0
    assert after.execute("SELECT COUNT(*) FROM geo_solutions").fetchone()[0] == 0
    assert after.execute("SELECT COUNT(*) FROM emitter_solutions").fetchone()[0] == 1
    after.close()


def test_measurement_rows_follow_detection_and_coverage(tmp_path: Path) -> None:
    database = tmp_path / "db.sqlite3"
    connection = build_database(database)
    seed_run(connection, run_id="run", latitude=32.052, longitude=34.802, transmitters=[NEAR, FAR])
    seed_run(
        connection,
        run_id="narrow",
        latitude=32.052,
        longitude=34.802,
        transmitters=[NEAR, FAR],
        usable_low_hz=867_500_000.0,
        usable_high_hz=868_000_000.0,
        coverage_status="partial",
    )
    connection.close()
    _declare(database)
    _declare(database, emitter_key="far", frequency_hz=FAR.frequency_hz)

    connection = connect_emitter_database(database)
    rows = {row["emitter_key"]: row for row in build_emitter_run_measurements(connection, "run")}
    assert rows["near"]["detected"] is True
    assert rows["near"]["usability"] == USABILITY_USABLE
    assert rows["near"]["attribution"] == ATTRIBUTION_FREQUENCY_MATCH
    assert rows["far"]["detected"] is False
    assert rows["far"]["usability"] == USABILITY_USABLE, "a continuous carrier's absence is evidence"

    narrow = {
        row["emitter_key"]: row for row in build_emitter_run_measurements(connection, "narrow")
    }
    assert narrow["far"]["usability"] == USABILITY_NOT_COVERED
    assert "partial_coverage" in narrow["near"]["quality_flags"]
    connection.close()


def test_an_intermittent_emitters_absence_is_kept_but_not_used(tmp_path: Path) -> None:
    database = tmp_path / "db.sqlite3"
    connection = build_database(database)
    seed_run(connection, run_id="run", latitude=32.052, longitude=34.802, transmitters=[NEAR, FAR])
    connection.close()
    _declare(database, emission=EMISSION_INTERMITTENT)
    _declare(database, emitter_key="far", frequency_hz=FAR.frequency_hz, emission="intermittent")

    connection = connect_emitter_database(database)
    rows = {row["emitter_key"]: row for row in build_emitter_run_measurements(connection, "run")}
    connection.close()
    assert rows["near"]["usability"] == USABILITY_USABLE, "a detection is used either way"
    assert rows["far"]["usability"] == USABILITY_ABSENCE_NOT_EVIDENCE
    assert "not transmitting" in rows["far"]["exclusion_reason"]


def test_emitters_within_tolerance_of_each_other_are_ambiguous(tmp_path: Path) -> None:
    database = tmp_path / "db.sqlite3"
    connection = build_database(database)
    seed_run(connection, run_id="run", latitude=32.052, longitude=34.802, transmitters=[NEAR])
    connection.close()
    _declare(database)
    _declare(database, emitter_key="twin", frequency_hz=NEAR.frequency_hz + 3_000.0)

    connection = connect_emitter_database(database)
    rows = {row["emitter_key"]: row for row in build_emitter_run_measurements(connection, "run")}
    connection.close()
    for key in ("near", "twin"):
        assert rows[key]["attribution"] == ATTRIBUTION_AMBIGUOUS_NEIGHBOUR
        assert rows[key]["usability"] == USABILITY_AMBIGUOUS
        assert rows[key]["attribution_detail"]


def test_full_chain_locates_the_emitter_and_scores_it_against_a_known_position(
    tmp_path: Path,
) -> None:
    database = tmp_path / "db.sqlite3"
    _seed(database)
    _declare(
        database,
        truth_latitude=NEAR.latitude,
        truth_longitude=NEAR.longitude,
        truth_source="synthetic scenario",
    )
    _declare(database, emitter_key="unheard", frequency_hz=866_000_100.0 + 400_000.0)

    report = solve_emitters(
        database_path=database, output_root=tmp_path / "out", settings=fast_solve_settings()
    )
    solutions = {row["emitter_key"]: row for row in report["solutions"]}
    near = solutions["near"]
    assert near["status"] == "ok"
    assert near["source_model"] == "single_transmitter_assumed"
    assert haversine_m(
        near["mode_latitude"], near["mode_longitude"], NEAR.latitude, NEAR.longitude
    ) < 2000.0
    assert near["truth"]["mode_error_m"] < 2000.0
    assert near["truth"]["inside"]["0.9"] is True
    assert near["truth"]["truth_source"] == "synthetic scenario"

    # Never heard anywhere: every stop is a non-detection, so there is not
    # enough evidence -- reported, not dropped.
    assert solutions["unheard"]["status"] == "insufficient_evidence"

    reports = tmp_path / "out" / "reports"
    assert (reports / f"emitters_{report['solve_batch_id']}.json").exists()
    geojson = json.loads((reports / f"emitters_{report['solve_batch_id']}.geojson").read_text())
    assert geojson["features"]

    connection = connect_emitter_database(database)
    stored = {row["emitter_key"]: row for row in latest_emitter_solutions(connection)}
    connection.close()
    assert json.loads(stored["near"]["truth_json"])["inside"]["0.9"] is True


def test_the_known_position_is_never_an_input_to_the_solve(tmp_path: Path) -> None:
    database = tmp_path / "db.sqlite3"
    _seed(database)
    results = []
    for latitude, longitude in ((NEAR.latitude, NEAR.longitude), (31.0, 35.5), (None, None)):
        _declare(
            database,
            truth_latitude=latitude,
            truth_longitude=longitude,
            truth_source="test" if latitude is not None else "",
        )
        report = solve_emitters(database_path=database, settings=fast_solve_settings())
        near = report["solutions"][0]
        results.append((near["mode_latitude"], near["mode_longitude"], near["area_km2_90"]))
    assert results[0] == results[1] == results[2]


def test_an_emitter_with_nothing_usable_is_listed_with_a_reason(tmp_path: Path) -> None:
    database = tmp_path / "db.sqlite3"
    connection = build_database(database)
    seed_run(connection, run_id="run", latitude=None, longitude=None, transmitters=[NEAR])
    connection.close()
    _declare(database)
    report = solve_emitters(database_path=database, settings=fast_solve_settings())
    assert report["solutions"][0]["status"] == STATUS_NO_MEASUREMENTS
    assert report["solutions"][0]["excluded_count"] == 1


def test_declaration_is_validated(tmp_path: Path) -> None:
    connection = connect_emitter_database(tmp_path / "db.sqlite3")
    with pytest.raises(EmitterError, match="truth-source"):
        upsert_emitter(
            connection, emitter_key="a", frequency_hz=1e8, truth_latitude=32.0, truth_longitude=34.0
        )
    with pytest.raises(EmitterError, match="both latitude and longitude"):
        upsert_emitter(connection, emitter_key="a", frequency_hz=1e8, truth_latitude=32.0)
    with pytest.raises(EmitterError, match="emission"):
        upsert_emitter(connection, emitter_key="a", frequency_hz=1e8, emission="bursty")
    with pytest.raises(EmitterError, match="frequency"):
        upsert_emitter(connection, emitter_key="a", frequency_hz=-1.0)
    upsert_emitter(connection, emitter_key="a", frequency_hz=1e8, label="first")
    upsert_emitter(connection, emitter_key="a", frequency_hz=2e8, label="second")
    emitters = list_emitters(connection)
    assert [(e["emitter_key"], e["frequency_hz"], e["label"]) for e in emitters] == [
        ("a", 2e8, "second")
    ]
    remove_emitter(connection, "a")
    assert list_emitters(connection) == []
    with pytest.raises(EmitterError):
        remove_emitter(connection, "a")
    connection.close()


def test_rebuild_reports_gain_reference_and_summary(tmp_path: Path) -> None:
    database = tmp_path / "db.sqlite3"
    _seed(database, STOPS[:4])
    _declare(database)
    report = materialise_emitter_measurements(database_path=database)
    assert report["run_count"] == 4
    assert report["summary"]["detections"] == 4
    assert report["reference_gain"] is not None


def test_cli_add_list_solve_and_remove(tmp_path: Path) -> None:
    database = tmp_path / "db.sqlite3"
    _seed(database)
    added = runner.invoke(
        app,
        [
            "geo", "emitter", "add", "near",
            "--frequency-mhz", f"{NEAR.frequency_hz / 1e6:.6f}",
            "--truth-lat", str(NEAR.latitude), "--truth-lon", str(NEAR.longitude),
            "--truth-source", "synthetic",
            "--database", str(database),
        ],
    )
    assert added.exit_code == 0, added.output
    listed = runner.invoke(app, ["geo", "emitter", "list", "--database", str(database)])
    assert listed.exit_code == 0 and "near" in listed.output

    measured = runner.invoke(app, ["geo", "emitter", "measurements", "--database", str(database)])
    assert measured.exit_code == 0, measured.output

    solved = runner.invoke(
        app, ["geo", "emitter", "solve", "--database", str(database), "--solve-batch-id", "b1"]
    )
    assert solved.exit_code == 0, solved.output
    assert "near" in solved.output and "inside 90%" in solved.output

    bad = runner.invoke(
        app,
        ["geo", "emitter", "add", "x", "--frequency-mhz", "100", "--emission", "sometimes",
         "--database", str(database)],
    )
    assert bad.exit_code == 1

    removed = runner.invoke(app, ["geo", "emitter", "remove", "near", "--database", str(database)])
    assert removed.exit_code == 0
