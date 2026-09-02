"""Per-stop common-mode offsets, and the two-pass solve that uses them."""

from __future__ import annotations

import math
from pathlib import Path

import pytest
from fixtures.geo_scenario import Transmitter, fast_solve_settings, seed_run

from dmr_iq_surveyor.geo.commonmode import (
    STATUS_ESTIMATED,
    STATUS_INCONSISTENT,
    STATUS_NOT_ESTIMABLE,
    STATUS_WITHIN_NOISE,
    CommonModeSettings,
    estimate_offsets,
    residuals_by_run,
    summarise,
)
from dmr_iq_surveyor.geo.model import haversine_m
from dmr_iq_surveyor.geo.pipeline import (
    import_reference_sites,
    materialise_measurements,
    solve_all_sites,
)
from dmr_iq_surveyor.geo.store import connect_geo_database

# Six sites spread across the area, so most stops hear several of them -- which
# is the condition under which a shared offset is identifiable at all.
TRANSMITTERS = [
    Transmitter(866_712_500.0, 32.050, 34.800, reference_level_db=30.0, path_loss_exponent=3.2),
    Transmitter(866_925_000.0, 32.090, 34.850, reference_level_db=30.0, path_loss_exponent=3.2),
    Transmitter(867_025_000.0, 32.020, 34.760, reference_level_db=30.0, path_loss_exponent=3.2),
    Transmitter(867_762_500.0, 32.075, 34.780, reference_level_db=30.0, path_loss_exponent=3.2),
    Transmitter(868_050_000.0, 32.035, 34.845, reference_level_db=30.0, path_loss_exponent=3.2),
    Transmitter(868_425_000.0, 32.060, 34.815, reference_level_db=30.0, path_loss_exponent=3.2),
]

STOPS = [
    (32.045, 34.795), (32.062, 34.812), (32.038, 34.828), (32.072, 34.788),
    (32.028, 34.772), (32.085, 34.838), (32.015, 34.808), (32.098, 34.802),
    (32.052, 34.752), (32.048, 34.870),
]


def _csv() -> str:
    header = "wacn_hex,system_id_hex,rfss,site,observation_status,primary_cc_mhz,nac_hex,notes\n"
    rows = "".join(
        f"BEE00,37D,1,{20 + index},DIRECT,{tx.frequency_hz / 1e6:.6f},37B,\n"
        for index, tx in enumerate(TRANSMITTERS)
    )
    return header + rows


def _campaign(tmp_path: Path, *, offset_run: str | None = None, offset_db: float = 0.0) -> Path:
    database = tmp_path / "db.sqlite3"
    connect_geo_database(database).close()
    csv_path = tmp_path / "sites.csv"
    csv_path.write_text(_csv(), encoding="utf-8")
    import_reference_sites(csv_path, database_path=database, snapshot_id="cm")

    connection = connect_geo_database(database)
    for index, (latitude, longitude) in enumerate(STOPS):
        seed_run(
            connection,
            run_id=f"stop_{index:02d}",
            latitude=latitude,
            longitude=longitude,
            transmitters=TRANSMITTERS,
            site_id=f"stop_{index:02d}",
            capture_start_utc=f"2026-08-01T{7 + index:02d}:00:00+00:00",
        )
    if offset_run is not None:
        # Every site at this one stop shifts together: an antenna knocked out
        # of position, or local interference raising the noise floor.
        connection.execute(
            "UPDATE rf_observations SET snr_db = snr_db + ? WHERE survey_run_id = ?",
            (offset_db, offset_run),
        )
        connection.commit()
    connection.close()
    materialise_measurements(database_path=database)
    return database


# ------------------------------------------------------------------ estimator


def test_a_shared_offset_is_found_and_a_lone_site_is_not_guessed_at() -> None:
    offsets = estimate_offsets(
        {
            "stop_0": [0.3, -0.5, 0.8, 0.1],
            "stop_1": [-0.2, 0.4, -0.6, 0.2],
            "stop_2": [0.1, 0.0, -0.3, 0.5],
            "stop_bad": [-7.1, -6.8, -7.4, -6.9],
            "stop_thin": [1.2],
        }
    )
    assert offsets["stop_bad"].status == STATUS_ESTIMATED
    assert offsets["stop_bad"].offset_db == pytest.approx(-7.0, abs=0.3)
    assert offsets["stop_bad"].scatter_db < 0.5
    assert offsets["stop_bad"].applied is True
    assert offsets["stop_0"].status == STATUS_WITHIN_NOISE
    assert offsets["stop_0"].offset_db == 0.0

    thin = offsets["stop_thin"]
    assert thin.status == STATUS_NOT_ESTIMABLE
    assert thin.applied is False
    assert "told apart from one site" in thin.reason
    assert summarise(offsets)["applied"] == 1


def test_offsets_are_centred_so_only_differences_between_stops_count() -> None:
    """Adding a constant to every stop and to every reference level is an
    identical fit, so an absolute offset is not identifiable."""
    shifted = estimate_offsets(
        {f"stop_{i}": [12.0, 12.2, 11.8, 12.1] for i in range(4)}
    )
    assert all(offset.offset_db == 0.0 for offset in shifted.values())
    assert all(offset.status == STATUS_WITHIN_NOISE for offset in shifted.values())


def test_the_median_ignores_one_badly_fitting_site() -> None:
    offsets = estimate_offsets(
        {
            "stop_a": [-5.0, -5.2, -4.8, 22.0],
            "stop_b": [0.1, -0.1, 0.2, 0.0],
            "stop_c": [0.0, 0.1, -0.2, 0.1],
        }
    )
    assert offsets["stop_a"].offset_db == pytest.approx(-5.0, abs=0.5)


def test_a_large_but_inconsistent_shift_is_refused(tmp_path: Path) -> None:
    """"Common" is the whole claim, so it is tested rather than assumed.

    A big median residual whose sites disagree wildly is model misfit at one
    stop; correcting it would bend real geometry to suit a fitting error.
    """
    offsets = estimate_offsets(
        {
            # A large median shift that the sites plainly do not share.
            "stop_scattered": [-14.0, -12.0, -1.0, 0.0],
            "stop_a": [0.1, -0.1, 0.2, 0.0],
            "stop_b": [0.0, 0.1, -0.2, 0.1],
            "stop_c": [0.1, 0.0, 0.1, -0.1],
        }
    )
    scattered = offsets["stop_scattered"]
    assert scattered.status == STATUS_INCONSISTENT
    assert scattered.applied is False
    assert scattered.offset_db == 0.0
    assert "not a shift they share" in scattered.reason


def test_a_disabled_estimator_reports_but_does_not_apply() -> None:
    offsets = estimate_offsets(
        {"stop_bad": [-7.0] * 4, "stop_a": [0.0] * 4, "stop_b": [0.1] * 4},
        CommonModeSettings(enabled=False),
    )
    assert offsets["stop_bad"].status == STATUS_ESTIMATED
    assert offsets["stop_bad"].applied is False


def test_residuals_are_collected_only_from_detections_of_solved_sites() -> None:
    solutions = [
        {
            "status": "ok",
            "residuals": [
                {"survey_run_id": "a", "detected": True, "residual_db": 1.0},
                {"survey_run_id": "a", "detected": False, "exceedance_db": 0.0},
                {"survey_run_id": "b", "detected": True, "residual_db": -2.0},
            ],
        },
        {"status": "insufficient_evidence", "residuals": []},
    ]
    assert residuals_by_run(solutions) == {"a": [1.0], "b": [-2.0]}


# ------------------------------------------------------------- two-pass solve


def test_a_stop_knocked_off_the_campaign_is_corrected(tmp_path: Path) -> None:
    database = _campaign(tmp_path, offset_run="stop_04", offset_db=-8.0)
    report = solve_all_sites(database_path=database, settings=fast_solve_settings())
    common = report["common_mode"]

    assert common["applied"] >= 1, f"the 8 dB stop should have been caught: {common}"
    corrected = common["offsets"]["stop_04"]
    assert corrected["applied"] is True
    # The magnitude is a LOWER bound: the first fitting pass already absorbed
    # part of the shift into each site's reference level and position, so what
    # is left in the residuals understates it. Sign and identification are what
    # matter; a second correction pass would recover more at twice the cost.
    assert -9.0 < corrected["offset_db"] < -3.0
    assert corrected["site_count"] >= 3
    assert "predicted level" in corrected["reason"]

    solved = [row for row in report["solutions"] if row["status"] == "ok"]
    assert solved, "the campaign should still solve"
    assert any(
        any("common-mode" in warning for warning in row.get("warnings", [])) for row in solved
    ), "a corrected solution must say so"


def test_a_consistent_campaign_is_left_alone(tmp_path: Path) -> None:
    database = _campaign(tmp_path)
    report = solve_all_sites(database_path=database, settings=fast_solve_settings())
    assert report["common_mode"]["applied"] == 0
    assert report["common_mode"]["largest_offset_db"] < 2.0
    assert not any(
        "common-mode" in warning
        for row in report["solutions"]
        for warning in row.get("warnings", [])
    )


def test_correcting_a_bad_stop_improves_the_estimates(tmp_path: Path) -> None:
    """The point of the correction, measured rather than asserted."""
    truth = {
        f"BEE00:37D:1:{20 + index}": (tx.latitude, tx.longitude)
        for index, tx in enumerate(TRANSMITTERS)
    }

    def median_error(database: Path, *, common_mode: CommonModeSettings) -> float:
        report = solve_all_sites(
            database_path=database,
            settings=fast_solve_settings(),
            common_mode=common_mode,
        )
        errors = [
            haversine_m(row["mode_latitude"], row["mode_longitude"], *truth[row["site_key"]])
            for row in report["solutions"]
            if row["status"] == "ok" and row.get("mode_latitude") is not None
        ]
        assert errors, "the campaign must solve at least one site"
        return float(sorted(errors)[len(errors) // 2])

    database = _campaign(tmp_path, offset_run="stop_04", offset_db=-9.0)
    uncorrected = median_error(database, common_mode=CommonModeSettings(enabled=False))
    corrected = median_error(database, common_mode=CommonModeSettings(enabled=True))
    assert corrected <= uncorrected + 1.0, (
        f"correcting an 9 dB stop should not make things worse: "
        f"{uncorrected:.0f} m -> {corrected:.0f} m"
    )


def test_the_plan_ranks_places_whose_outcome_is_least_predictable(tmp_path: Path) -> None:
    database = _campaign(tmp_path)
    report = solve_all_sites(database_path=database, settings=fast_solve_settings())
    plan = report["plan"]

    assert plan["status"] == "ok"
    assert plan["top_stops"], "a solved campaign should have somewhere worth going"
    assert all(0.0 <= stop["value"] <= 1.0 for stop in plan["top_stops"])
    assert plan["top_stops"] == sorted(
        plan["top_stops"], key=lambda stop: stop["value"], reverse=True
    )

    # Suggestions are spread, not three cells of one hot spot.
    for first in range(len(plan["top_stops"])):
        for second in range(first + 1, len(plan["top_stops"])):
            a, b = plan["top_stops"][first], plan["top_stops"][second]
            assert haversine_m(a["latitude"], a["longitude"], b["latitude"], b["longitude"]) > 1000.0

    # A suggestion beside a stop already made would repeat a measurement.
    for stop in plan["top_stops"]:
        nearest = min(
            haversine_m(stop["latitude"], stop["longitude"], latitude, longitude)
            for latitude, longitude in STOPS
        )
        assert nearest > 200.0

    for stop in plan["top_stops"]:
        assert stop["helps_most"], "a suggestion must say which sites it helps"


def test_planning_refuses_before_there_is_anything_to_plan_against(tmp_path: Path) -> None:
    database = tmp_path / "db.sqlite3"
    connect_geo_database(database).close()
    csv_path = tmp_path / "sites.csv"
    csv_path.write_text(_csv(), encoding="utf-8")
    import_reference_sites(csv_path, database_path=database, snapshot_id="cm")
    report = solve_all_sites(database_path=database, settings=fast_solve_settings())
    assert report["plan"]["status"] == "no_targets"
    assert "spread around the area" in report["plan"]["reason"]


def test_the_report_explains_the_correction_and_the_plan(tmp_path: Path) -> None:
    database = _campaign(tmp_path, offset_run="stop_04", offset_db=-8.0)
    output = tmp_path / "out"
    report = solve_all_sites(
        database_path=database,
        output_root=output,
        solve_batch_id="b1",
        settings=fast_solve_settings(),
    )
    markdown = (output / "reports" / "geolocation_b1.md").read_text(encoding="utf-8")
    assert "common-mode" in markdown.lower()
    assert "Where to go next" in markdown
    assert "least predictable" in markdown
    assert math.isfinite(report["common_mode"]["largest_offset_db"])
