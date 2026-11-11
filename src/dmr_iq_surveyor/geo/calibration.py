"""Measure whether geolocation credible regions are honest (Phase 8, Stage 2).

The product of the solver is a credible region, so "accuracy" is two
questions, and both have to be answered:

1. **Honesty** -- does a 90% region contain the transmitter in about 90% of
   independent trials? Below that the method is overconfident, which is the
   worst failure this tool can have: a small, wrong polygon.
2. **Usefulness** -- given honesty, how large is the region and how far is
   the mode from the truth?

`run_calibration` answers both by simulation: it draws trials from a
declared scenario, solves each with the production `solve_site`, unchanged,
and compares the answer with the position the trial was generated from.
`truth_check` is the same comparison for one real solution against a known
position, used by `geo emitter solve` for field validation.

Nothing here feeds back into the solver. Truth is only ever compared with a
result after the fact.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Any

import numpy as np

from dmr_iq_surveyor.geo.model import GeoMeasurement, LocalProjection, SolveSettings
from dmr_iq_surveyor.geo.solver import (
    STATUS_INSUFFICIENT_EVIDENCE,
    STATUS_OK,
    STATUS_UNBOUNDED_REGION,
    STATUS_WEAK_GEOMETRY,
    SolveResult,
    highest_density_threshold,
    solve_site,
)

LAYOUT_RING = "ring"
LAYOUT_RANDOM = "random"
LAYOUT_ONE_SIDED = "one_sided"
LAYOUT_LINE = "line"
LAYOUT_DRIVE_LOOP = "drive_loop"
LAYOUTS = (LAYOUT_RING, LAYOUT_RANDOM, LAYOUT_ONE_SIDED, LAYOUT_LINE, LAYOUT_DRIVE_LOOP)

VERDICT_CONSISTENT = "consistent"
VERDICT_OVERCONFIDENT = "overconfident"
VERDICT_CONSERVATIVE = "conservative"
VERDICT_TOO_FEW_TRIALS = "too_few_trials"

# Below this many trials with a surface, a coverage rate is anecdote.
MIN_TRIALS_FOR_VERDICT = 10

# Where the synthetic scenario is anchored. Only local metres matter to the
# solver; a real latitude keeps the projection's scale factor realistic.
_ANCHOR_LATITUDE = 32.0
_ANCHOR_LONGITUDE = 34.8


@dataclass(slots=True)
class CalibrationScenario:
    """Everything a simulated campaign is drawn from, stored with the report."""

    stops: int = 8
    layout: str = LAYOUT_RING
    # Typical distance from the scenario centre to a stop.
    radius_m: float = 4000.0
    # Relative random spread of each stop's radius and angle (ring layouts).
    radius_jitter: float = 0.3
    # The transmitter is placed uniformly in a disc of this radius around
    # the centre, so it is not always where the stops are symmetric about.
    transmitter_spread_m: float = 1500.0
    # True level at the reference distance (1 km), in the detector's units.
    reference_level_db: float = 30.0
    true_path_loss_exponent: float = 3.5
    true_sigma_db: float = 6.0
    # 0 means independent shadowing per stop. A positive value draws
    # shadowing with exponential spatial correlation exp(-d / correlation_m)
    # (Gudmundson), which the solver's likelihood does not model.
    correlation_m: float = 0.0
    # The detector's threshold in the same units: below it a stop records a
    # non-detection, exactly as a real survey run does.
    censor_level_db: float = 6.0
    # Fraction of stops whose level is additionally off by a large amount
    # (blocked, or with unusual line of sight), drawn N(0, outlier_sigma_db).
    # Applied before censoring, as the real effect would be.
    outlier_fraction: float = 0.0
    outlier_sigma_db: float = 20.0
    # Spacing of stops along the loop for the `drive_loop` layout.
    drive_spacing_m: float = 150.0
    trials: int = 40
    seed: int = 0

    def validate(self) -> None:
        if self.layout not in LAYOUTS:
            raise ValueError(f"layout must be one of {list(LAYOUTS)}, got {self.layout!r}")
        if self.stops < 2:
            raise ValueError("at least two stops are required")
        if self.radius_m <= 0:
            raise ValueError("radius_m must be positive")
        if not 0 <= self.radius_jitter < 1:
            raise ValueError("radius_jitter must be in [0, 1)")
        if self.transmitter_spread_m < 0:
            raise ValueError("transmitter_spread_m must be non-negative")
        if self.true_path_loss_exponent <= 0:
            raise ValueError("true_path_loss_exponent must be positive")
        if self.true_sigma_db < 0:
            raise ValueError("true_sigma_db must be non-negative")
        if self.correlation_m < 0:
            raise ValueError("correlation_m must be non-negative")
        if not 0.0 <= self.outlier_fraction <= 1.0:
            raise ValueError("outlier_fraction must be in [0, 1]")
        if self.outlier_sigma_db < 0:
            raise ValueError("outlier_sigma_db must be non-negative")
        if self.drive_spacing_m <= 0:
            raise ValueError("drive_spacing_m must be positive")
        if self.trials < 1:
            raise ValueError("at least one trial is required")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class TrialOutcome:
    trial: int
    status: str
    detections: int
    non_detections: int
    mode_error_m: float | None = None
    mean_error_m: float | None = None
    area_km2: dict[str, float] = field(default_factory=dict)
    inside: dict[str, bool] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _level_key(level: float) -> str:
    return f"{level:g}"


def _stop_positions(scenario: CalibrationScenario, rng: np.random.Generator) -> np.ndarray:
    """Stop positions in local metres around the scenario centre, shape (n, 2)."""
    n = scenario.stops
    if scenario.layout == LAYOUT_DRIVE_LOOP:
        # A loop driven at the scenario radius, sampled every `drive_spacing_m`
        # starting at a random point: `stops` consecutive bins, so a short
        # drive covers only an arc of the loop, exactly as a real one would.
        circumference = 2.0 * math.pi * scenario.radius_m
        start = rng.uniform(0.0, 2.0 * math.pi)
        step = scenario.drive_spacing_m / scenario.radius_m
        angles = start + step * np.arange(n)
        if n * scenario.drive_spacing_m > circumference:
            angles = start + np.linspace(0.0, 2.0 * math.pi, n, endpoint=False)
        radii = np.full(n, scenario.radius_m)
    elif scenario.layout == LAYOUT_RANDOM:
        radii = scenario.radius_m * np.sqrt(rng.uniform(0.05, 1.0, n)) * 1.4
        angles = rng.uniform(0.0, 2.0 * math.pi, n)
    elif scenario.layout == LAYOUT_LINE:
        offsets = np.linspace(-scenario.radius_m, scenario.radius_m, n)
        offsets = offsets + rng.normal(0.0, scenario.radius_m * scenario.radius_jitter * 0.1, n)
        heading = rng.uniform(0.0, math.pi)
        # The line passes beside the centre, not through it.
        across = scenario.radius_m * 0.5
        x = offsets * math.cos(heading) - across * math.sin(heading)
        y = offsets * math.sin(heading) + across * math.cos(heading)
        return np.column_stack([x, y])
    else:
        span = 2.0 * math.pi if scenario.layout == LAYOUT_RING else math.pi / 2.0
        start = rng.uniform(0.0, 2.0 * math.pi)
        base = start + span * (np.arange(n) / (n if scenario.layout == LAYOUT_RING else n - 1))
        jitter = scenario.radius_jitter * span / max(n, 1) / 2.0
        angles = base + rng.uniform(-jitter, jitter, n)
        radii = scenario.radius_m * (1.0 + rng.uniform(-1.0, 1.0, n) * scenario.radius_jitter)
    return np.column_stack([radii * np.cos(angles), radii * np.sin(angles)])


def _shadowing(
    positions: np.ndarray, scenario: CalibrationScenario, rng: np.random.Generator
) -> np.ndarray:
    n = positions.shape[0]
    if scenario.true_sigma_db == 0:
        return np.zeros(n)
    if scenario.correlation_m <= 0:
        return rng.normal(0.0, scenario.true_sigma_db, n)
    deltas = positions[:, None, :] - positions[None, :, :]
    distances = np.hypot(deltas[..., 0], deltas[..., 1])
    covariance = scenario.true_sigma_db**2 * np.exp(-distances / scenario.correlation_m)
    # A tiny diagonal load keeps the factorisation stable for near-coincident stops.
    covariance += np.eye(n) * 1e-9 * scenario.true_sigma_db**2
    factor = np.linalg.cholesky(covariance)
    return factor @ rng.normal(0.0, 1.0, n)


def simulate_measurements(
    scenario: CalibrationScenario, rng: np.random.Generator, settings: SolveSettings
) -> tuple[list[GeoMeasurement], tuple[float, float]]:
    """One synthetic campaign: measurements plus the true (lat, lon)."""
    projection = LocalProjection(_ANCHOR_LATITUDE, _ANCHOR_LONGITUDE)
    radius = scenario.transmitter_spread_m * math.sqrt(rng.uniform(0.0, 1.0))
    angle = rng.uniform(0.0, 2.0 * math.pi)
    tx_x, tx_y = radius * math.cos(angle), radius * math.sin(angle)
    positions = _stop_positions(scenario, rng)
    distances = np.maximum(
        np.hypot(positions[:, 0] - tx_x, positions[:, 1] - tx_y), settings.min_distance_m
    )
    levels = (
        scenario.reference_level_db
        - 10.0
        * scenario.true_path_loss_exponent
        * np.log10(distances / settings.reference_distance_m)
        + _shadowing(positions, scenario, rng)
    )
    # Always drawn, so turning outliers on or off never shifts the random
    # stream every later draw depends on.
    outlier_draw = rng.uniform(0.0, 1.0, levels.size)
    outlier_offset = rng.normal(0.0, scenario.outlier_sigma_db, levels.size)
    levels = levels + np.where(outlier_draw < scenario.outlier_fraction, outlier_offset, 0.0)
    latitudes, longitudes = projection.to_geographic(positions[:, 0], positions[:, 1])
    measurements = []
    for index, (latitude, longitude, level) in enumerate(
        zip(np.atleast_1d(latitudes), np.atleast_1d(longitudes), levels, strict=True)
    ):
        detected = bool(level >= scenario.censor_level_db)
        measurements.append(
            GeoMeasurement(
                label=f"stop{index}",
                latitude=float(latitude),
                longitude=float(longitude),
                detected=detected,
                level_db=float(level) if detected else None,
                censor_level_db=scenario.censor_level_db,
                survey_run_id=f"stop{index}",
            )
        )
    truth_latitude, truth_longitude = projection.to_geographic(tx_x, tx_y)
    return measurements, (float(truth_latitude), float(truth_longitude))


def truth_check(
    result: SolveResult,
    truth_latitude: float,
    truth_longitude: float,
    levels: tuple[float, ...],
) -> dict[str, Any]:
    """Compare one solution with a known transmitter position.

    `inside_<level>` asks whether the known position falls in the
    highest-density region holding that mass. A position outside the fine
    grid is outside every region: the fine grid already spans the box that
    holds almost all of the coarse posterior's mass.
    """
    report: dict[str, Any] = {
        "truth_latitude": truth_latitude,
        "truth_longitude": truth_longitude,
        "mode_error_m": None,
        "mean_error_m": None,
        "inside": {},
        "truth_in_analysed_grid": False,
    }
    if result.surface is None:
        report["note"] = f"no posterior surface ({result.status}); nothing to compare"
        return report
    surface = result.surface
    projection = surface.projection
    truth_x, truth_y = (float(value) for value in projection.to_local(truth_latitude, truth_longitude))
    for name, latitude, longitude in (
        ("mode_error_m", result.mode_latitude, result.mode_longitude),
        ("mean_error_m", result.mean_latitude, result.mean_longitude),
    ):
        if latitude is None or longitude is None:
            continue
        x, y = (float(value) for value in projection.to_local(latitude, longitude))
        report[name] = float(math.hypot(x - truth_x, y - truth_y))

    grid = surface.grid
    column = round((truth_x - grid.x_min) / grid.resolution_m)
    row = round((truth_y - grid.y_min) / grid.resolution_m)
    in_grid = 0 <= column < grid.nx and 0 <= row < grid.ny
    report["truth_in_analysed_grid"] = in_grid
    probability_at_truth = (
        float(surface.probability[row * grid.nx + column]) if in_grid else 0.0
    )
    for level in levels:
        threshold = highest_density_threshold(surface.probability, level)
        report["inside"][_level_key(level)] = bool(
            in_grid and probability_at_truth >= threshold and probability_at_truth > 0.0
        )
    return report


def _verdict(hits: int, total: int, nominal: float) -> dict[str, Any]:
    if total == 0:
        return {
            "nominal": nominal,
            "trials": 0,
            "covered": 0,
            "coverage": None,
            "standard_error": None,
            "verdict": VERDICT_TOO_FEW_TRIALS,
        }
    coverage = hits / total
    standard_error = math.sqrt(max(nominal * (1.0 - nominal), 1e-12) / total)
    if total < MIN_TRIALS_FOR_VERDICT:
        verdict = VERDICT_TOO_FEW_TRIALS
    elif coverage < nominal - 2.0 * standard_error:
        verdict = VERDICT_OVERCONFIDENT
    elif coverage > nominal + 2.0 * standard_error:
        verdict = VERDICT_CONSERVATIVE
    else:
        verdict = VERDICT_CONSISTENT
    return {
        "nominal": nominal,
        "trials": total,
        "covered": hits,
        "coverage": round(coverage, 4),
        "standard_error": round(standard_error, 4),
        "verdict": verdict,
    }


def _percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    return float(np.percentile(np.asarray(values, dtype=float), q))


def _coverage_block(
    outcomes: list[TrialOutcome], levels: tuple[float, ...]
) -> dict[str, Any]:
    block = {}
    for level in levels:
        key = _level_key(level)
        hits = sum(1 for outcome in outcomes if outcome.inside.get(key))
        block[key] = _verdict(hits, len(outcomes), level)
    return block


def run_calibration(
    scenario: CalibrationScenario,
    settings: SolveSettings | None = None,
    *,
    keep_trials: bool = False,
) -> dict[str, Any]:
    """Draw `scenario.trials` campaigns, solve each, and score the answers.

    Coverage is reported twice: over solves the solver called `ok`, which is
    what a user acts on, and over every solve that produced a surface, which
    includes the refusals that still drew a region. A method can look honest
    on the first only because it refuses the hard cases -- the status counts
    say how often that happened.
    """
    scenario.validate()
    resolved = settings or SolveSettings()
    resolved.validate()
    levels = tuple(sorted(resolved.credible_levels))
    children = np.random.SeedSequence(scenario.seed).spawn(scenario.trials)

    outcomes: list[TrialOutcome] = []
    for index, child in enumerate(children):
        rng = np.random.default_rng(child)
        measurements, (truth_latitude, truth_longitude) = simulate_measurements(
            scenario, rng, resolved
        )
        result = solve_site(measurements, resolved)
        check = truth_check(result, truth_latitude, truth_longitude, levels)
        areas: dict[str, float] = {}
        if result.surface is not None:
            cell_area_km2 = (result.surface.grid.resolution_m / 1000.0) ** 2
            for level in levels:
                threshold = highest_density_threshold(result.surface.probability, level)
                areas[_level_key(level)] = float(
                    np.count_nonzero(result.surface.probability >= threshold) * cell_area_km2
                )
        outcomes.append(
            TrialOutcome(
                trial=index,
                status=result.status,
                detections=result.detection_count,
                non_detections=result.non_detection_count,
                mode_error_m=check["mode_error_m"],
                mean_error_m=check["mean_error_m"],
                area_km2=areas,
                inside=dict(check["inside"]),
            )
        )

    statuses = {
        status: sum(1 for outcome in outcomes if outcome.status == status)
        for status in (
            STATUS_OK,
            STATUS_UNBOUNDED_REGION,
            STATUS_WEAK_GEOMETRY,
            STATUS_INSUFFICIENT_EVIDENCE,
        )
    }
    ok = [outcome for outcome in outcomes if outcome.status == STATUS_OK]
    surfaced = [outcome for outcome in outcomes if outcome.mode_error_m is not None]
    widest = _level_key(max(levels))

    def accuracy(selected: list[TrialOutcome]) -> dict[str, Any]:
        errors = [outcome.mode_error_m for outcome in selected if outcome.mode_error_m is not None]
        areas = [outcome.area_km2[widest] for outcome in selected if widest in outcome.area_km2]
        return {
            "trials": len(selected),
            "mode_error_m_median": _percentile(errors, 50),
            "mode_error_m_p90": _percentile(errors, 90),
            f"area_km2_{widest}_median": _percentile(areas, 50),
        }

    report: dict[str, Any] = {
        "scenario": scenario.to_dict(),
        "settings": resolved.to_dict(),
        "trials": len(outcomes),
        "status_counts": statuses,
        "coverage_ok": _coverage_block(ok, levels),
        "coverage_all_surfaces": _coverage_block(surfaced, levels),
        "accuracy_ok": accuracy(ok),
        "accuracy_all_surfaces": accuracy(surfaced),
        "note": (
            "Simulated campaigns drawn from a log-distance model with the scenario's own "
            "parameters. Coverage close to nominal here is necessary, not sufficient: real "
            "propagation departs from the model, which only field truth can measure."
        ),
    }
    if keep_trials:
        report["trial_outcomes"] = [outcome.to_dict() for outcome in outcomes]
    return report


def render_calibration_markdown(report: dict[str, Any]) -> str:
    scenario = report["scenario"]
    lines = [
        "# Geolocation calibration",
        "",
        (
            f"Scenario: {scenario['stops']} stops, layout `{scenario['layout']}`, radius "
            f"{scenario['radius_m']:g} m, true n={scenario['true_path_loss_exponent']:g}, "
            f"true sigma={scenario['true_sigma_db']:g} dB, correlation "
            f"{scenario['correlation_m']:g} m, outliers {scenario['outlier_fraction'] * 100:g}% "
            f"(+/-{scenario['outlier_sigma_db']:g} dB), {report['trials']} trials, "
            f"seed {scenario['seed']}."
        ),
        "",
        "Status counts: "
        + ", ".join(f"{key}={value}" for key, value in report["status_counts"].items()),
        "",
        "| population | level | coverage | covered/trials | verdict |",
        "|---|---:|---:|---:|---|",
    ]
    for population, block in (
        ("ok", report["coverage_ok"]),
        ("all surfaces", report["coverage_all_surfaces"]),
    ):
        for key, entry in block.items():
            coverage = "n/a" if entry["coverage"] is None else f"{entry['coverage'] * 100:.1f}%"
            lines.append(
                f"| {population} | {float(key) * 100:.0f}% | {coverage} | "
                f"{entry['covered']}/{entry['trials']} | {entry['verdict']} |"
            )
    lines.append("")
    lines.append("| population | trials | median mode error | p90 mode error | median area |")
    lines.append("|---|---:|---:|---:|---:|")
    for population, block in (
        ("ok", report["accuracy_ok"]),
        ("all surfaces", report["accuracy_all_surfaces"]),
    ):
        area_key = next(key for key in block if key.startswith("area_km2_"))

        def show(value: float | None, unit: str, scale: float = 1.0) -> str:
            return "n/a" if value is None else f"{value * scale:,.0f} {unit}"

        area = block[area_key]
        lines.append(
            f"| {population} | {block['trials']} | {show(block['mode_error_m_median'], 'm')} | "
            f"{show(block['mode_error_m_p90'], 'm')} | "
            f"{'n/a' if area is None else f'{area:.2f} km2'} |"
        )
    lines.append("")
    lines.append(report["note"])
    return "\n".join(lines) + "\n"


__all__ = [
    "LAYOUTS",
    "VERDICT_CONSERVATIVE",
    "VERDICT_CONSISTENT",
    "VERDICT_OVERCONFIDENT",
    "VERDICT_TOO_FEW_TRIALS",
    "CalibrationScenario",
    "TrialOutcome",
    "render_calibration_markdown",
    "run_calibration",
    "simulate_measurements",
    "truth_check",
]
