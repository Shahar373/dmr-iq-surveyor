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
    USABILITY_LEVEL_UNRELIABLE,
    USABILITY_NO_POSITION,
    USABILITY_NOT_COVERED,
    USABILITY_RECEIVER_ARTIFACT,
    USABILITY_SUPERSEDED_CHANNEL,
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
        "level_unreliable": 0,
        "receiver_artifact": 0,
        "superseded_channel": 0,
        "run_excluded": 0,
    }


def test_a_dc_artifact_detection_is_not_evidence_about_a_transmitter(tmp_path: Path) -> None:
    """The tuner's own LO spike sits at the centre frequency at every stop.

    Matched to a site it would inject the same confident, wrong level into
    every single measurement -- the worst failure this method can have.
    """
    connection = build_database(tmp_path / "db.sqlite3")
    seed_run(connection, run_id="run", latitude=32.05, longitude=34.80, transmitters=[NEAR])
    connection.execute(
        "UPDATE rf_observations SET dc_warning = 1 WHERE survey_run_id = 'run'"
    )
    connection.commit()
    row = _rows(connection)["BEE00:37D:1:30"]
    assert row["usability"] == USABILITY_RECEIVER_ARTIFACT
    assert "DC/LO artifact" in row["exclusion_reason"]
    assert "dc_warning" in row["quality_flags"]
    connection.close()


def test_edge_warning_is_flagged_but_does_not_exclude(tmp_path: Path) -> None:
    """`edge_warning` marks a FIXED 150 kHz margin from the recording's Nyquist
    edges, not anything measured.

    At a 200 kS/s rate that margin covers the whole band, so excluding on it
    threw away every detection in the run. The measured usable passband is the
    honest edge test and runs separately.
    """
    connection = build_database(tmp_path / "db.sqlite3")
    seed_run(connection, run_id="run", latitude=32.05, longitude=34.80, transmitters=[NEAR])
    connection.execute(
        "UPDATE rf_observations SET edge_warning = 1 WHERE survey_run_id = 'run'"
    )
    connection.commit()
    row = _rows(connection)["BEE00:37D:1:30"]
    assert row["usability"] == USABILITY_USABLE
    assert "edge_warning" in row["quality_flags"]
    connection.close()


def test_a_detection_outside_the_measured_passband_is_not_used_as_a_level(tmp_path: Path) -> None:
    connection = build_database(tmp_path / "db.sqlite3")
    seed_run(connection, run_id="run", latitude=32.05, longitude=34.80, transmitters=[NEAR])
    # The channel WAS detected; the measured passband is then found to stop
    # short of it, so its level is understated by the receiver roll-off.
    connection.execute(
        "UPDATE survey_runs SET usable_low_hz = ?, usable_high_hz = ? WHERE survey_run_id = 'run'",
        (867_800_000.0, 868_500_000.0),
    )
    connection.commit()
    row = _rows(connection)["BEE00:37D:1:30"]
    assert row["detected"] is True
    assert row["usability"] == USABILITY_LEVEL_UNRELIABLE
    assert "detected_outside_measured_passband" in row["quality_flags"]
    connection.close()


def test_one_site_with_two_channels_contributes_one_measurement_per_stop(tmp_path: Path) -> None:
    """Two channels of one site at one place are not independent evidence."""
    connection = build_database(tmp_path / "db.sqlite3")
    site_id = connection.execute(
        "SELECT p25_site_id FROM p25_sites WHERE site_key = 'BEE00:37D:1:30'"
    ).fetchone()["p25_site_id"]
    connection.execute(
        "INSERT INTO p25_site_channels(p25_site_id, frequency_hz, role, evidence, snapshot_id) "
        "VALUES (?, ?, 'primary_control', 'external_snapshot', 'test_snapshot')",
        (site_id, 866_500_000.0),
    )
    connection.commit()
    seed_run(
        connection,
        run_id="run",
        latitude=32.05,
        longitude=34.80,
        transmitters=[NEAR, Transmitter(866_500_000.0, 32.05, 34.80, reference_level_db=20.0)],
    )
    rows = [
        row
        for row in build_run_measurements(connection, "run")
        if row["site_key"] == "BEE00:37D:1:30"
    ]
    assert len(rows) == 2, "both channels are still recorded"
    usable = [row for row in rows if row["usability"] == USABILITY_USABLE]
    superseded = [row for row in rows if row["usability"] == USABILITY_SUPERSEDED_CHANNEL]
    assert len(usable) == 1, "but only one may be used as evidence"
    assert len(superseded) == 1
    assert "not independent evidence" in superseded[0]["exclusion_reason"]
    # The stronger of the two detections is the one kept.
    assert usable[0]["level_db"] >= superseded[0]["level_db"]
    connection.close()


def test_an_excluded_run_contributes_nothing_but_keeps_its_reason(tmp_path: Path) -> None:
    """A truncated capture must not become a non-detection.

    A signal that was there but was not recorded long enough to be detected
    would otherwise become evidence that pushes the site AWAY from that stop --
    a confident wrong measurement rather than a missing one.
    """
    from dmr_iq_surveyor.geo.measurements import USABILITY_RUN_EXCLUDED
    from dmr_iq_surveyor.geo.store import clear_run_exclusion, exclude_run, run_exclusion

    connection = build_database(tmp_path / "db.sqlite3")
    seed_run(connection, run_id="run", latitude=32.05, longitude=34.80, transmitters=[NEAR, FAR])
    exclude_run(connection, "run", "capture ended at 38% of the requested duration")
    assert run_exclusion(connection, "run") is not None

    rows = build_run_measurements(connection, "run")
    assert rows, "the rows are still recorded"
    assert all(row["usability"] == USABILITY_RUN_EXCLUDED for row in rows)
    assert all("38%" in row["exclusion_reason"] for row in rows)
    assert summarise(rows)["usable"] == 0

    clear_run_exclusion(connection, "run")
    assert any(row["usability"] == USABILITY_USABLE for row in build_run_measurements(connection, "run"))
    connection.close()


def test_a_stop_recorded_at_a_different_gain_is_flagged_across_the_campaign(
    tmp_path: Path,
) -> None:
    """Levels at different receiver gain are not on one scale, and the method
    is a comparison of levels between places."""
    from dmr_iq_surveyor.geo.pipeline import materialise_measurements
    from dmr_iq_surveyor.geo.store import connect_geo_database, fetch_all_measurements

    database = tmp_path / "db.sqlite3"
    connection = build_database(database)
    for index, gain in enumerate([40.0, 40.0, 40.0, 33.0]):
        seed_run(
            connection,
            run_id=f"run_{index}",
            latitude=32.04 + index * 0.01,
            longitude=34.79 + index * 0.01,
            transmitters=[NEAR],
            site_id=f"stop_{index}",
            gain=gain,
        )
    connection.close()

    result = materialise_measurements(database_path=database)
    assert result["reference_gain"] == 40.0
    assert result["gain_drift_runs"] == ["run_3"]

    connection = connect_geo_database(database)
    import json as _json

    flagged = {
        row["survey_run_id"]
        for row in fetch_all_measurements(connection)
        if any(f.startswith("gain_differs") for f in _json.loads(row["quality_flags_json"]))
    }
    connection.close()
    assert flagged == {"run_3"}
