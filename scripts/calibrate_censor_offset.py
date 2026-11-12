#!/usr/bin/env python3
"""What a wrong censor level costs, measured with the `geo calibrate` engine.

The simulated detector switches at the scenario's `censor_level_db`; the
solver is told `censor_level_db - delta`. Everything else -- the scenario
draws, `solve_site`, the scoring -- is `geo.calibration.run_calibration`
unchanged, on the same seed for every delta, so rows differ only by delta.

    python scripts/calibrate_censor_offset.py --scenario random16 --deltas 0,0.5,1.5,3

Results are recorded in docs/phase8-fixed-emitter-geolocation.md (Stage 3.1).
Each row takes about a minute on the test-grade grid with 40 trials.
"""

from __future__ import annotations

import argparse
import dataclasses
import json

import dmr_iq_surveyor.geo.calibration as calibration
from dmr_iq_surveyor.geo.model import SolveSettings

SCENARIOS = {
    "random16": {"stops": 16, "layout": "random", "reference_level_db": 30.0},
    "ring12_weak": {"stops": 12, "layout": "ring", "reference_level_db": 27.0},
    "drive40": {
        "stops": 40,
        "layout": "drive_loop",
        "drive_spacing_m": 150.0,
        "radius_m": 3000.0,
        "reference_level_db": 27.0,
        "correlation_m": 500.0,
    },
}


def grid_settings(correlation_m: float) -> SolveSettings:
    """The grid the Phase 8 tables were measured on."""
    return SolveSettings(
        margin_m=8000.0,
        coarse_resolution_m=800.0,
        resolution_m=200.0,
        target_fine_cells=4000,
        max_fine_cells=20_000,
        max_coarse_cells=20_000,
        shadowing_correlation_m=correlation_m,
    )


def run(scenario_name: str, delta_db: float, trials: int, seed: int, model_l: float) -> dict:
    original = calibration.simulate_measurements

    def understated(scenario, rng, settings):
        measurements, truth = original(scenario, rng, settings)
        return [
            dataclasses.replace(item, censor_level_db=item.censor_level_db - delta_db)
            for item in measurements
        ], truth

    calibration.simulate_measurements = understated
    try:
        report = calibration.run_calibration(
            calibration.CalibrationScenario(
                trials=trials, seed=seed, censor_level_db=6.0, **SCENARIOS[scenario_name]
            ),
            grid_settings(model_l),
            keep_trials=True,
        )
    finally:
        calibration.simulate_measurements = original
    coverage = report["coverage_all_surfaces"]
    accuracy = report["accuracy_all_surfaces"]
    return {
        "scenario": scenario_name,
        "delta_db": delta_db,
        "coverage_50": coverage["0.5"]["coverage"],
        "coverage_90": coverage["0.9"]["coverage"],
        "verdict_90": coverage["0.9"]["verdict"],
        "mode_error_m_median": accuracy["mode_error_m_median"],
        "mode_error_m_p90": accuracy["mode_error_m_p90"],
        "area_km2_90_median": accuracy["area_km2_0.9_median"],
        "non_detections_mean": sum(
            outcome["non_detections"] for outcome in report["trial_outcomes"]
        ) / trials,
        "status_counts": report["status_counts"],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--scenario", choices=sorted(SCENARIOS), default="random16")
    parser.add_argument("--deltas", default="0,0.5,1.5,3", help="comma-separated, in dB")
    parser.add_argument("--trials", type=int, default=40)
    parser.add_argument("--seed", type=int, default=11)
    parser.add_argument("--model-correlation-m", type=float, default=500.0)
    args = parser.parse_args()
    for delta in (float(value) for value in args.deltas.split(",")):
        row = run(args.scenario, delta, args.trials, args.seed, args.model_correlation_m)
        print(json.dumps(row), flush=True)


if __name__ == "__main__":
    main()
