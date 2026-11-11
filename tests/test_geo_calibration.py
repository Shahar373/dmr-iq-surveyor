"""Phase 8 Stage 2: the calibration harness measures honesty, not just size."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from dmr_iq_surveyor.cli_app import app
from dmr_iq_surveyor.geo.calibration import (
    VERDICT_CONSERVATIVE,
    VERDICT_CONSISTENT,
    VERDICT_OVERCONFIDENT,
    VERDICT_TOO_FEW_TRIALS,
    CalibrationScenario,
    render_calibration_markdown,
    run_calibration,
    simulate_measurements,
    truth_check,
)
from dmr_iq_surveyor.geo.model import SolveSettings
from dmr_iq_surveyor.geo.solver import solve_site

runner = CliRunner()


def _fast() -> SolveSettings:
    return SolveSettings(
        margin_m=8000.0,
        coarse_resolution_m=800.0,
        resolution_m=200.0,
        target_fine_cells=4000,
        max_fine_cells=20_000,
        max_coarse_cells=20_000,
    )


# Every stop hears the carrier (no censored terms), which keeps each solve
# fast. 300 m spacing along a 3 km loop, 30 stops.
DRIVE = {
    "trials": 20,
    "reference_level_db": 45.0,
    "radius_m": 3000.0,
    "stops": 30,
    "layout": "drive_loop",
    "drive_spacing_m": 300.0,
}


def test_the_harness_is_deterministic_for_a_seed() -> None:
    scenario = CalibrationScenario(trials=4, reference_level_db=40.0, stops=10, seed=7)
    first = run_calibration(scenario, _fast(), keep_trials=True)
    second = run_calibration(scenario, _fast(), keep_trials=True)
    assert first["trial_outcomes"] == second["trial_outcomes"]
    # Adding trials never changes the earlier ones.
    longer = run_calibration(
        CalibrationScenario(trials=6, reference_level_db=40.0, stops=10, seed=7),
        _fast(),
        keep_trials=True,
    )
    assert longer["trial_outcomes"][:4] == first["trial_outcomes"]


def test_independent_shadowing_in_the_models_family_is_not_overconfident() -> None:
    report = run_calibration(CalibrationScenario(correlation_m=0.0, **DRIVE), _fast())
    entry = report["coverage_all_surfaces"]["0.9"]
    assert entry["trials"] == DRIVE["trials"]
    assert entry["verdict"] in (VERDICT_CONSISTENT, VERDICT_CONSERVATIVE)


def test_correlated_shadowing_along_a_drive_is_caught_as_overconfident() -> None:
    """The independence assumption is the solver's, not the world's.

    Drive bins a few hundred metres apart share their shadowing; a
    likelihood that multiplies them as independent evidence draws regions
    far too small. This is the measured motivation for Stage 4.
    """
    report = run_calibration(CalibrationScenario(correlation_m=600.0, **DRIVE), _fast())
    assert report["coverage_all_surfaces"]["0.9"]["verdict"] == VERDICT_OVERCONFIDENT


def test_truth_check_scores_inside_and_outside() -> None:
    import numpy as np

    rng = np.random.default_rng(3)
    scenario = CalibrationScenario(reference_level_db=45.0, stops=12, true_sigma_db=2.0)
    measurements, (latitude, longitude) = simulate_measurements(scenario, rng, _fast())
    result = solve_site(measurements, _fast())
    near = truth_check(result, latitude, longitude, (0.5, 0.9))
    assert near["truth_in_analysed_grid"] is True
    assert near["inside"]["0.9"] is True
    assert near["mode_error_m"] is not None and near["mode_error_m"] < 2000.0

    far = truth_check(result, latitude + 1.0, longitude, (0.5, 0.9))
    assert far["inside"] == {"0.5": False, "0.9": False}
    assert far["mode_error_m"] > 100_000.0


def test_truth_check_without_a_surface_says_why() -> None:
    from dmr_iq_surveyor.geo.solver import SolveResult

    result = SolveResult(
        status="insufficient_evidence", status_reason="", detection_count=1, non_detection_count=0
    )
    check = truth_check(result, 32.0, 34.8, (0.9,))
    assert check["inside"] == {}
    assert "insufficient_evidence" in check["note"]


def test_too_few_trials_get_no_verdict() -> None:
    report = run_calibration(
        CalibrationScenario(trials=3, reference_level_db=40.0, stops=10), _fast()
    )
    for block in (report["coverage_ok"], report["coverage_all_surfaces"]):
        for entry in block.values():
            assert entry["verdict"] == VERDICT_TOO_FEW_TRIALS
    markdown = render_calibration_markdown(report)
    assert "Geolocation calibration" in markdown and "too_few_trials" in markdown


@pytest.mark.parametrize(
    ("field", "value"),
    [("layout", "spiral"), ("stops", 1), ("trials", 0), ("correlation_m", -1.0)],
)
def test_scenarios_are_validated(field: str, value: object) -> None:
    with pytest.raises(ValueError):
        CalibrationScenario(**{field: value}).validate()


@pytest.mark.parametrize("layout", ["ring", "random", "one_sided", "line", "drive_loop"])
def test_every_layout_simulates(layout: str) -> None:
    import numpy as np

    scenario = CalibrationScenario(layout=layout, stops=6)
    measurements, _ = simulate_measurements(scenario, np.random.default_rng(0), _fast())
    assert len(measurements) == 6


def test_cli_calibrate_writes_a_report(tmp_path: Path) -> None:
    output = tmp_path / "calibration.json"
    result = runner.invoke(
        app,
        [
            "geo", "calibrate", "--trials", "2", "--stops", "8",
            "--reference-level-db", "40", "--output", str(output),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Geolocation calibration" in result.output
    report = json.loads(output.read_text())
    assert report["trials"] == 2
    assert report["scenario"]["stops"] == 8

    bad = runner.invoke(app, ["geo", "calibrate", "--layout", "spiral", "--trials", "1"])
    assert bad.exit_code == 1


# --- Stage 4: correlated shadowing in the likelihood -------------------------


def test_shadowing_weights_count_a_cluster_as_about_one_stop() -> None:
    import numpy as np

    from dmr_iq_surveyor.geo.solver import shadowing_weights

    assert shadowing_weights(np.zeros(3), np.zeros(3), 0.0) is None
    east = np.array([0.0, 0.0, 0.0, 50_000.0])
    north = np.zeros(4)
    weights = shadowing_weights(east, north, 300.0)
    assert weights is not None
    assert np.allclose(weights[:3], 1.0 / 3.0)
    assert weights[3] == pytest.approx(1.0)


def test_repeating_a_stop_no_longer_shrinks_the_region() -> None:
    """Three copies of one measurement are one measurement, not three."""
    import numpy as np

    rng = np.random.default_rng(5)
    scenario = CalibrationScenario(reference_level_db=40.0, stops=8, true_sigma_db=4.0)
    once, _ = simulate_measurements(scenario, rng, _fast())
    thrice = [m for m in once for _ in range(3)]

    def area(measurements, correlation_m: float) -> float:
        settings = _fast()
        settings.shadowing_correlation_m = correlation_m
        result = solve_site(measurements, settings)
        assert result.surface is not None
        cell = (result.surface.grid.resolution_m / 1000.0) ** 2
        probability = result.surface.probability
        from dmr_iq_surveyor.geo.solver import highest_density_threshold

        return float(np.count_nonzero(probability >= highest_density_threshold(probability, 0.9)))* cell

    independent_once, independent_thrice = area(once, 0.0), area(thrice, 0.0)
    assert independent_thrice < 0.7 * independent_once, "the old likelihood is fooled by repeats"
    weighted_once, weighted_thrice = area(once, 300.0), area(thrice, 300.0)
    assert weighted_thrice == pytest.approx(weighted_once, rel=0.15)


def test_correlation_off_reproduces_the_phase7_solver_exactly() -> None:
    import numpy as np

    rng = np.random.default_rng(9)
    measurements, _ = simulate_measurements(
        CalibrationScenario(reference_level_db=30.0, stops=8), rng, _fast()
    )
    baseline = solve_site(measurements, _fast())
    explicit = _fast()
    explicit.shadowing_correlation_m = 0.0
    again = solve_site(measurements, explicit)
    assert baseline.surface is not None and again.surface is not None
    assert np.array_equal(baseline.surface.probability, again.surface.probability)
    assert baseline.mode_latitude == again.mode_latitude
    assert again.diagnostics["shadowing_correlation"]["effective_measurements"] == 8.0


def test_modelling_the_correlation_restores_honest_coverage_on_a_drive() -> None:
    settings = _fast()
    settings.shadowing_correlation_m = 600.0
    report = run_calibration(CalibrationScenario(correlation_m=600.0, **DRIVE), settings)
    assert report["coverage_all_surfaces"]["0.9"]["verdict"] in (
        VERDICT_CONSISTENT,
        VERDICT_CONSERVATIVE,
    )
