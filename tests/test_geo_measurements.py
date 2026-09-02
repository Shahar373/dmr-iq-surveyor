"""Measurement extraction: the attribution ladder and the usability rules."""

from __future__ import annotations

from pathlib import Path

import pytest
from fixtures.geo_scenario import Transmitter, build_database, seed_run

from dmr_iq_surveyor.geo.measurements import (
    ATTRIBUTION_AMBIGUOUS_REUSE,
    ATTRIBUTION_INFERRED_UNIQUE,
    POSITION_SOURCE_RUN_GPS,
    USABILITY_AMBIGUOUS,
    USABILITY_NO_POSITION,
    USABILITY_NOT_COVERED,
    USABILITY_USABLE,
    MeasurementSettings,
    build_run_measurements,
    summarise,
)
from dmr_iq_surveyor.geo.pipeline import materialise_measurements
from dmr_iq_surveyor.geo.store import fetch_all_measurements

# Site 30 is close to the stop below and heard; site 33 is far and not.
NEAR = Transmitter(867_762_500.0, 32.050, 34.800, reference_level_db=30.0)
FAR = Transmitter(866_712_500.0, 32.600, 35.300, reference_level_db=30.0)
SHARED = Transmitter(867_912_500.0, 32.050, 34.800, reference_level_db=30.0)


def _rows(connection, run_id: str = "run", **kwargs):
    return {row["site_key"]: row for row in build_run_measurements(connection, run_id, **kwargs)}


def test_detection_non_detection_and_ambiguity(tmp_path: Path) -> None:
    connection = build_database(tmp_path / "db.sqlite3")
    seed_run(
        connection,
        run_id="run",
        latitude=32.052,
        longitude=34.802,
        transmitters=[NEAR, FAR, SHARED],
    )
    rows = _rows(connection)

    heard = rows["BEE00:37D:1:30"]
    assert heard["detected"] is True
    assert heard["usability"] == USABILITY_USABLE
    assert heard["attribution"] == ATTRIBUTION_INFERRED_UNIQUE
    assert heard["level_db"] > heard["censor_level_db"]
    assert heard["position_source"] == POSITION_SOURCE_RUN_GPS

    missed = rows["BEE00:37D:1:33"]
    assert missed["detected"] is False
    assert missed["usability"] == USABILITY_USABLE, "a real non-detection is usable evidence"
    assert missed["level_db"] is None

    for key in ("BEE00:37D:1:50", "BEE00:37D:1:82"):
        assert rows[key]["attribution"] == ATTRIBUTION_AMBIGUOUS_REUSE
        assert rows[key]["usability"] == USABILITY_AMBIGUOUS
        assert "mixture" in rows[key]["attribution_detail"]
    connection.close()


def test_a_site_with_no_frequency_produces_no_measurement_row(tmp_path: Path) -> None:
    connection = build_database(tmp_path / "db.sqlite3")
    seed_run(connection, run_id="run", latitude=32.05, longitude=34.80, transmitters=[NEAR])
    assert "BEE00:37D:1:81" not in _rows(connection)
    connection.close()


def test_outside_the_measured_passband_is_not_a_non_detection(tmp_path: Path) -> None:
    """The distinction the whole design turns on: not looking is not evidence."""
    connection = build_database(tmp_path / "db.sqlite3")
    seed_run(
        connection,
        run_id="run",
        latitude=32.05,
        longitude=34.80,
        transmitters=[NEAR],
        usable_low_hz=867_500_000.0,
        usable_high_hz=868_000_000.0,
        coverage_status="partial",
    )
    rows = _rows(connection)
    assert rows["BEE00:37D:1:33"]["usability"] == USABILITY_NOT_COVERED
    assert "not the same as having looked" in rows["BEE00:37D:1:33"]["exclusion_reason"]
    assert rows["BEE00:37D:1:30"]["usability"] == USABILITY_USABLE
    assert "partial_coverage" in rows["BEE00:37D:1:30"]["quality_flags"]
    connection.close()


def test_passband_guard_pushes_edge_frequencies_out_of_evidence(tmp_path: Path) -> None:
    connection = build_database(tmp_path / "db.sqlite3")
    seed_run(
        connection,
        run_id="run",
        latitude=32.05,
        longitude=34.80,
        transmitters=[NEAR],
        usable_low_hz=866_700_000.0,
        usable_high_hz=869_000_000.0,
    )
    generous = _rows(connection, settings=MeasurementSettings(passband_guard_hz=0.0))
    assert generous["BEE00:37D:1:33"]["usability"] == USABILITY_USABLE
    guarded = _rows(connection, settings=MeasurementSettings(passband_guard_hz=25_000.0))
    assert guarded["BEE00:37D:1:33"]["usability"] == USABILITY_NOT_COVERED
    connection.close()


def test_a_run_without_a_position_cannot_be_placed(tmp_path: Path) -> None:
    connection = build_database(tmp_path / "db.sqlite3")
    seed_run(connection, run_id="run", latitude=None, longitude=None, transmitters=[NEAR])
    rows = _rows(connection)
    assert rows["BEE00:37D:1:30"]["usability"] == USABILITY_NO_POSITION
    assert rows["BEE00:37D:1:33"]["usability"] == USABILITY_NO_POSITION
    # An ambiguous frequency stays reported as ambiguous: that is a
    # permanent property of the registry, not something a later run with a
    # position would fix.
    assert rows["BEE00:37D:1:50"]["usability"] == USABILITY_AMBIGUOUS
    connection.close()


def test_missing_gain_is_flagged_not_hidden(tmp_path: Path) -> None:
    connection = build_database(tmp_path / "db.sqlite3")
    seed_run(
        connection, run_id="run", latitude=32.05, longitude=34.80, transmitters=[NEAR], gain=None
    )
    assert "not_gain_comparable" in _rows(connection)["BEE00:37D:1:30"]["quality_flags"]
    connection.close()


def test_censor_level_follows_the_chosen_metric(tmp_path: Path) -> None:
    connection = build_database(tmp_path / "db.sqlite3")
    seed_run(
        connection,
        run_id="run",
        latitude=32.05,
        longitude=34.80,
        transmitters=[NEAR],
        threshold_db=6.0,
    )
    average = _rows(connection)["BEE00:37D:1:30"]
    p95 = _rows(connection, settings=MeasurementSettings(level_metric="p95_snr_db"))[
        "BEE00:37D:1:30"
    ]
    assert average["censor_level_db"] == 6.0
    assert p95["censor_level_db"] == 11.0
    assert p95["level_db"] == pytest.approx(average["level_db"] + 4.0)
    connection.close()


def test_unknown_level_metric_is_rejected() -> None:
    with pytest.raises(ValueError, match="level_metric"):
        MeasurementSettings(level_metric="rssi_dbm").validate()


def test_rematerialising_replaces_rather_than_accumulates(tmp_path: Path) -> None:
    database = tmp_path / "db.sqlite3"
    connection = build_database(database)
    seed_run(connection, run_id="run", latitude=32.05, longitude=34.80, transmitters=[NEAR])
    connection.close()

    first = materialise_measurements(database_path=database)
    second = materialise_measurements(database_path=database)
    assert first["summary"] == second["summary"]

    from dmr_iq_surveyor.geo.store import connect_geo_database

    connection = connect_geo_database(database)
    assert len(fetch_all_measurements(connection)) == first["summary"]["total"]
    connection.close()


def test_summarise_counts_each_category_once() -> None:
    rows = [
        {"usability": USABILITY_USABLE, "detected": True},
        {"usability": USABILITY_USABLE, "detected": False},
        {"usability": USABILITY_NOT_COVERED, "detected": False},
        {"usability": USABILITY_AMBIGUOUS, "detected": False},
        {"usability": USABILITY_NO_POSITION, "detected": False},
    ]
    assert summarise(rows) == {
        "total": 5,
        "usable": 2,
        "detections": 1,
        "non_detections": 1,
        "not_covered": 1,
        "ambiguous": 1,
        "no_position": 1,
    }
