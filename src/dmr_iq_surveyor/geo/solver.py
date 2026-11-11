"""Bayesian grid posterior over a transmitter's position (Phase 7).

Given measurements of one site made at several places -- detections with a
level, and non-detections that only bound the level from above -- this
computes a posterior probability surface for the transmitter's position,
marginalising the unknown reference level `P0` and the unknown path-loss
exponent `n`.

Why a posterior rather than a least-squares point fit is argued in
`docs/phase7-geolocation-design.md`. In short: RSSI localisation is
genuinely multimodal, non-detections are censored rather than missing, and
"the region shrinks as sessions accumulate" is what a product of
likelihoods does by construction.

Runtime and memory are bounded the same way the IQ stages bound theirs: a
coarse pass over the whole region, a fine pass restricted to where the mass
actually is, and cell-chunked evaluation so peak memory does not scale with
the grid.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np
from scipy.special import log_ndtr

from dmr_iq_surveyor.geo.model import (
    GeoMeasurement,
    Grid,
    LocalProjection,
    SolveSettings,
    azimuth_span_deg,
    bearing_deg,
    build_grid,
)

# `log_ndtr` is exact but costs roughly an order of magnitude more per
# element than an interpolation, and the censored term is evaluated once
# per (grid cell, non-detection, path-loss exponent, reference level) --
# hundreds of millions of times for a routine solve. The function is
# smooth, so a dense uniform table interpolates it to about 1e-6, far below
# the several-dB shadow-fading term it feeds.
#
# The table is uniformly spaced so the lookup is index arithmetic rather
# than `np.interp`'s binary search. Below the table the value is about
# -1250 in log space -- exactly zero probability in float64 -- so clamping
# there cannot create a spurious flat tail that would make a credible
# region look unbounded.
_LOG_NDTR_LOW = -50.0
_LOG_NDTR_HIGH = 10.0
_LOG_NDTR_COUNT = 20_001
_LOG_NDTR_VALUES = log_ndtr(np.linspace(_LOG_NDTR_LOW, _LOG_NDTR_HIGH, _LOG_NDTR_COUNT))
# Stored as slopes rather than looking up index+1: one fancy-index
# fewer over arrays with millions of elements.
_LOG_NDTR_SLOPES = np.append(np.diff(_LOG_NDTR_VALUES), 0.0)
_LOG_NDTR_INVERSE_STEP = (_LOG_NDTR_COUNT - 1) / (_LOG_NDTR_HIGH - _LOG_NDTR_LOW)


def tabulated_log_ndtr(z: np.ndarray) -> np.ndarray:
    """Interpolated `log Phi(z)`, matching `scipy.special.log_ndtr` to
    around 1e-6 over the range where it can still affect a posterior."""
    position = np.asarray(z)
    position = (position - _LOG_NDTR_LOW) * _LOG_NDTR_INVERSE_STEP
    np.clip(position, 0.0, _LOG_NDTR_COUNT - 2, out=position)
    index = position.astype(np.intp)
    position -= index
    position *= _LOG_NDTR_SLOPES[index]
    position += _LOG_NDTR_VALUES[index]
    return position


STATUS_OK = "ok"
STATUS_INSUFFICIENT_EVIDENCE = "insufficient_evidence"
STATUS_UNBOUNDED_REGION = "unbounded_region"
STATUS_WEAK_GEOMETRY = "weak_geometry"

# Recorded on every solution. The estimator fits one point source; a P25
# simulcast site is several transmitters keyed together and has no single
# position to find. Phase 7 does not model that, and says so in the output
# rather than leaving the assumption implicit.
SOURCE_MODEL = "single_transmitter_assumed"

# Generic maturity/validation state of THIS ESTIMATOR, independent of any
# particular band, protocol or deployment. The solver is not P25- or
# 868 MHz-specific -- it takes a log-distance model and censored/detected
# levels -- so these constants say only "the method has not been checked
# against ground truth", never which frequency range that applies to. A
# caller with a specific workflow in mind (e.g. the P25/868 MHz field
# campaign) states that context itself, in its own report or UI text.
GEOLOCATION_MATURITY = "experimental"
VALIDATION_STATUS = "unvalidated"

# Whether a solution's fitted propagation parameters are separable from each
# other. Four states, because they are four different things to a reader and
# collapsing them loses the reason:
#   identified      -- enough detections; the numbers describe the data
#   underdetermined -- a fit ran, but the exponent and reference level are
#                      reproduced exactly by many pairs, so the numbers are
#                      arithmetic rather than measurement
#   not_fitted      -- no fit was attempted; there are no numbers to qualify
#   unknown         -- solved before this check existed, so nothing can be
#                      said either way (the migration's default, never written)
FIT_IDENTIFIED = "identified"
FIT_UNDERDETERMINED = "underdetermined"
FIT_NOT_FITTED = "not_fitted"
FIT_UNKNOWN = "unknown"


@dataclass(slots=True)
class PosteriorSurface:
    grid: Grid
    projection: LocalProjection
    probability: np.ndarray  # normalised, shape (ny, nx)

    def reshaped(self) -> np.ndarray:
        return self.probability.reshape(self.grid.ny, self.grid.nx)


@dataclass(slots=True)
class SolveResult:
    status: str
    status_reason: str
    detection_count: int
    non_detection_count: int
    warnings: list[str] = field(default_factory=list)
    surface: PosteriorSurface | None = None
    mode_latitude: float | None = None
    mode_longitude: float | None = None
    mean_latitude: float | None = None
    mean_longitude: float | None = None
    path_loss_exponent: float | None = None
    # Whether the fitted parameters above describe the data or merely
    # reproduce it. "underdetermined" means there were too few detections for
    # the exponent and reference level to be separable, so they are arithmetic,
    # not measurement. The region is unaffected and remains honest.
    fit_status: str = FIT_NOT_FITTED
    reference_level_db: float | None = None
    residual_rms_db: float | None = None
    residuals: list[dict[str, Any]] = field(default_factory=list)
    azimuth_span_deg: float | None = None
    diagnostics: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "status_reason": self.status_reason,
            "detection_count": self.detection_count,
            "non_detection_count": self.non_detection_count,
            "warnings": list(self.warnings),
            "mode_latitude": self.mode_latitude,
            "mode_longitude": self.mode_longitude,
            "mean_latitude": self.mean_latitude,
            "mean_longitude": self.mean_longitude,
            "path_loss_exponent": self.path_loss_exponent,
            "fit_status": self.fit_status,
            "reference_level_db": self.reference_level_db,
            "residual_rms_db": self.residual_rms_db,
            "residuals": list(self.residuals),
            "azimuth_span_deg": self.azimuth_span_deg,
            "source_model": SOURCE_MODEL,
            "diagnostics": dict(self.diagnostics),
        }


def _log_distance_ratio(
    cell_x: np.ndarray,
    cell_y: np.ndarray,
    point_x: np.ndarray,
    point_y: np.ndarray,
    settings: SolveSettings,
) -> np.ndarray | None:
    """`log10(d / d0)` from every cell in a chunk to every given point."""
    if point_x.size == 0:
        return None
    distance = np.hypot(cell_x[:, None] - point_x[None, :], cell_y[:, None] - point_y[None, :])
    np.maximum(distance, settings.min_distance_m, out=distance)
    return np.log10(distance / settings.reference_distance_m)


def reference_level_offsets(
    detection_count: float, settings: SolveSettings, sigma: float | None = None
) -> np.ndarray:
    """Offsets from each cell's own best-fit reference level to integrate over.

    `P0` is not marginalised on one global grid spanning the whole region.
    That grid would have to be both very wide (the implied `P0` varies by
    over a hundred dB across a 60 km area) and finer than the shadow-fading
    standard deviation, which is unaffordable. Instead each cell integrates
    over offsets around *its own* maximum-likelihood `P0`, where the
    detection term reduces exactly:

        sum_i (a_i - P0)^2  =  RSS(cell)  +  K * delta^2

    with `a_i` the per-detection implied reference level, `P0_hat = mean(a_i)`
    and `delta = P0 - P0_hat`. The `K delta^2` half is the same for every
    cell, so only `RSS(cell)` has to be computed per cell, and the offsets
    can be a small fixed vector.

    The window covers the detection posterior's own width (`sigma/sqrt(K)`)
    plus room for censored terms, which pull `P0` downwards by at most a few
    `sigma`. Trapezoidal integration of a smooth, rapidly decaying integrand
    converges geometrically in the step size, so a modest sample count is
    accurate here rather than merely convenient.

    The window scales with `sigma`, which matters once the solver
    marginalises over several: the integration step differs per sigma, and
    that step is part of the measure. `_evaluate_grid` accounts for it.
    """
    resolved_sigma = settings.sigma_db if sigma is None else float(sigma)
    spread = resolved_sigma / math.sqrt(max(detection_count, 1))
    half_width = (
        settings.reference_level_window_sigma * spread
        + settings.reference_level_window_sigma * resolved_sigma
    )
    return np.linspace(-half_width, half_width, settings.reference_level_samples)


def _evaluate_grid(
    grid: Grid,
    *,
    detection_x: np.ndarray,
    detection_y: np.ndarray,
    detection_levels: np.ndarray,
    censored_x: np.ndarray,
    censored_y: np.ndarray,
    censored_levels: np.ndarray,
    settings: SolveSettings,
    detection_weights: np.ndarray | None = None,
    censored_weights: np.ndarray | None = None,
    detection_position_sigma: np.ndarray | None = None,
    censored_position_sigma: np.ndarray | None = None,
) -> tuple[np.ndarray, float, float, float, int, np.ndarray]:
    """Marginal log posterior per cell, the best (n, sigma, P0, cell), and
    the log mass each sigma collected.

    Chunked over cells so peak memory stays bounded no matter how large the
    grid is: the chunk size comes from `max_working_elements` divided by the
    per-cell working set, so one number bounds memory rather than it varying
    with how many non-detections a site happens to have.

    Constants that do not depend on cell, `n`, `sigma` or `P0` are dropped
    -- the `K delta^2` offset term and the `2 pi` in the Gaussian normaliser
    -- since they cancel in both the marginalisation and the final
    normalisation. The parts of the normaliser that DO depend on sigma are
    kept; see `log_measure` below for why that is not optional.

    With weights (`SolveSettings.shadowing_correlation_m > 0`) every
    detection's squared residual and every censored term is multiplied by
    its weight, and the detection COUNT becomes the weight SUM wherever it
    appears -- the reference-level mean, the `K delta^2` term, the offset
    window and sigma's normaliser. The algebra is otherwise identical: the
    per-cell `P0` is the weighted mean and `RSS` the weighted sum. Without
    weights the original unweighted expressions run unchanged, so a Phase 7
    solve is bit-for-bit what it was.
    """
    xs, ys = grid.axes()
    exponents = np.asarray(settings.path_loss_exponents, dtype=float)
    sigmas = np.asarray(settings.sigma_db_values, dtype=float)
    detection_count = int(detection_levels.size)
    censored_count = int(censored_levels.size)
    weighted = detection_weights is not None
    # Position uncertainty (Phase 8, Stage 3.2). Only when some measurement
    # actually carries one; otherwise the original expressions run untouched.
    positional = any(
        values is not None and bool(np.any(values > 0.0))
        for values in (detection_position_sigma, censored_position_sigma)
    )
    # The number of independent detections the evidence is worth. Equal to
    # the count when unweighted.
    effective_detections: float = (
        float(np.sum(detection_weights)) if detection_weights is not None else detection_count
    )

    # The largest temporary is (cells x offsets): censored measurements are
    # accumulated one at a time rather than broadcast into a three-axis
    # array, which keeps peak memory independent of how many of them a site
    # has, at identical total arithmetic. The offset count is the same for
    # every sigma -- only the window WIDTH scales -- so the chunk size does
    # not depend on which sigma is being evaluated.
    per_cell_elements = max(1, settings.reference_level_samples)
    chunk = int(
        min(settings.chunk_cells, max(1, settings.max_working_elements // per_cell_elements))
    )

    log_marginal = np.empty(grid.cell_count, dtype=np.float64)
    sigma_log_mass = np.full(sigmas.size, -np.inf, dtype=np.float64)
    best_value = -np.inf
    best_exponent = float(exponents[0])
    best_sigma = float(sigmas[0])
    best_reference = 0.0
    best_cell = 0

    for start in range(0, grid.cell_count, chunk):
        stop = min(start + chunk, grid.cell_count)
        indices = np.arange(start, stop)
        cell_x = xs[indices % grid.nx]
        cell_y = ys[indices // grid.nx]

        detection_ratio = _log_distance_ratio(
            cell_x, cell_y, detection_x, detection_y, settings
        )
        censored_ratio = _log_distance_ratio(cell_x, cell_y, censored_x, censored_y, settings)

        chunk_accumulator = np.full(stop - start, -np.inf, dtype=np.float64)
        for sigma_index, sigma in enumerate(sigmas):
            sigma = float(sigma)
            two_sigma_squared = 2.0 * sigma * sigma
            offsets = reference_level_offsets(effective_detections, settings, sigma=sigma)
            scaled_offsets = offsets / sigma

            # Sigma's own measure, and the reason it cannot be dropped the way
            # the cell- and exponent-invariant constants are. Two terms:
            #
            #   -K log(sigma)   the Gaussian normaliser for K detections.
            #                   Without it a wider sigma explains ANY residual
            #                   more cheaply, so the largest sigma on the grid
            #                   would always win and the marginalisation would
            #                   be a fixed choice wearing a disguise.
            #   +log(step)      the `P0` integral is a Riemann sum over the
            #                   offsets, and the step scales with sigma, so a
            #                   wider window would otherwise collect more mass
            #                   purely for being sampled more widely.
            #
            # With no censored terms the integral has a closed form,
            # sigma * sqrt(2 pi / K), whose sigma-dependence is the same
            # +log(sigma); the constant factor is identical for every sigma
            # and drops out. Both branches therefore reduce to -(K-1) log
            # sigma, which is the right power: profiling out the unknown
            # reference level costs exactly one degree of freedom.
            log_measure = -effective_detections * math.log(sigma)
            if censored_ratio is None:
                log_measure += math.log(sigma)
            else:
                log_measure += math.log(float(offsets[1] - offsets[0]))

            for exponent in exponents:
                # a_i: the reference level that would fit detection i exactly
                # if the transmitter were in this cell with this exponent.
                fitted = detection_levels[None, :] + 10.0 * exponent * detection_ratio
                if positional:
                    marginal, peak, peak_offset, best_level = _positional_terms(
                        fitted=fitted,
                        detection_ratio=detection_ratio,
                        censored_ratio=censored_ratio,
                        censored_levels=censored_levels,
                        exponent=float(exponent),
                        sigma=sigma,
                        offsets=offsets,
                        log_measure=log_measure,
                        settings=settings,
                        detection_weights=detection_weights,
                        censored_weights=censored_weights,
                        detection_position_sigma=detection_position_sigma,
                        censored_position_sigma=censored_position_sigma,
                    )
                elif weighted:
                    best_level = (fitted * detection_weights[None, :]).sum(
                        axis=1
                    ) / effective_detections
                    residual_sum_squares = (
                        np.square(fitted - best_level[:, None]) * detection_weights[None, :]
                    ).sum(axis=1)
                else:
                    best_level = fitted.mean(axis=1)
                    residual_sum_squares = np.square(fitted - best_level[:, None]).sum(axis=1)
                if not positional and censored_ratio is None:
                    base = -residual_sum_squares / two_sigma_squared
                    # With no censored terms the offset integral is the same
                    # constant for every cell, so the marginal is the base
                    # term plus sigma's measure.
                    marginal = base + log_measure
                    peak = marginal
                    peak_offset = np.zeros_like(base)
                elif not positional:
                    base = -residual_sum_squares / two_sigma_squared
                    shift = (
                        censored_levels[None, :] + 10.0 * exponent * censored_ratio
                        - best_level[:, None]
                    ) / sigma
                    censored_term = np.zeros((stop - start, offsets.size), dtype=np.float64)
                    for column in range(censored_count):
                        term = tabulated_log_ndtr(
                            shift[:, column][:, None] - scaled_offsets[None, :]
                        )
                        if censored_weights is not None:
                            term *= float(censored_weights[column])
                        censored_term += term
                    joint = (
                        base[:, None]
                        - effective_detections * np.square(offsets)[None, :] / two_sigma_squared
                        + censored_term
                    )
                    marginal = np.logaddexp.reduce(joint, axis=1) + log_measure
                    argument = np.argmax(joint, axis=1)
                    peak = (
                        np.take_along_axis(joint, argument[:, None], axis=1).reshape(-1)
                        + log_measure
                    )
                    peak_offset = offsets[argument]

                np.logaddexp(chunk_accumulator, marginal, out=chunk_accumulator)
                sigma_log_mass[sigma_index] = np.logaddexp(
                    sigma_log_mass[sigma_index], float(np.logaddexp.reduce(marginal))
                )
                local = int(np.argmax(peak))
                if float(peak[local]) > best_value:
                    best_value = float(peak[local])
                    best_exponent = float(exponent)
                    best_sigma = sigma
                    best_reference = float(best_level[local] + peak_offset[local])
                    best_cell = start + local
        log_marginal[start:stop] = chunk_accumulator

    return log_marginal, best_exponent, best_sigma, best_reference, best_cell, sigma_log_mass


_LOG10_E_TIMES_10 = 10.0 / math.log(10.0)


def _positional_terms(
    *,
    fitted: np.ndarray,
    detection_ratio: np.ndarray,
    censored_ratio: np.ndarray | None,
    censored_levels: np.ndarray,
    exponent: float,
    sigma: float,
    offsets: np.ndarray,
    log_measure: float,
    settings: SolveSettings,
    detection_weights: np.ndarray | None,
    censored_weights: np.ndarray | None,
    detection_position_sigma: np.ndarray | None,
    censored_position_sigma: np.ndarray | None,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """The marginal for one (sigma, exponent) when positions are uncertain.

    A position known to within `s` metres, at distance `d` from the cell,
    moves the predicted level by about `(10 n / ln 10) * s / d` dB. That adds
    in quadrature to the shadowing, so each measurement's variance becomes
    `sigma^2 / u` with

        u = 1 / (1 + ((10 n / ln 10) * s / (sigma * d))^2)

    -- 1 far from the cell, small close to it. The likelihood is then the
    same Gaussian with per-cell, per-measurement weights `u` (times any
    shadowing-correlation weight): weighted `P0`, weighted residual sum, and
    each measurement's own normaliser `+ 1/2 log u`. A censored term's z is
    scaled by `sqrt(u)`. The closed-form `P0` integral gains `-1/2 log E`,
    with `E` the cell's weight sum, which is no longer a constant.

    What this fixes: a stop that passes right by a candidate cell no longer
    pins the transmitter to within metres of a centroid that is itself only
    known to within tens of metres.
    """
    slope = _LOG10_E_TIMES_10 * exponent
    distance = settings.reference_distance_m * np.power(10.0, detection_ratio)
    if detection_position_sigma is not None:
        inflation = slope * detection_position_sigma[None, :] / (sigma * distance)
        u = 1.0 / (1.0 + np.square(inflation))
    else:
        u = np.ones_like(fitted)
    strength = detection_weights[None, :] if detection_weights is not None else 1.0
    weights = u * strength
    effective = weights.sum(axis=1)
    best_level = (fitted * weights).sum(axis=1) / effective
    residual = (np.square(fitted - best_level[:, None]) * weights).sum(axis=1)
    base = -residual / (2.0 * sigma * sigma) + 0.5 * (np.log(u) * strength).sum(axis=1)

    if censored_ratio is None:
        marginal = base - 0.5 * np.log(effective) + log_measure
        return marginal, marginal, np.zeros_like(base), best_level

    censored_distance = settings.reference_distance_m * np.power(10.0, censored_ratio)
    if censored_position_sigma is not None:
        censored_inflation = (
            slope * censored_position_sigma[None, :] / (sigma * censored_distance)
        )
        root_u = 1.0 / np.sqrt(1.0 + np.square(censored_inflation))
    else:
        root_u = np.ones_like(censored_distance)
    shift = (
        censored_levels[None, :] + 10.0 * exponent * censored_ratio - best_level[:, None]
    ) / sigma
    scaled_offsets = offsets / sigma
    censored_term = np.zeros((fitted.shape[0], offsets.size), dtype=np.float64)
    for column in range(censored_levels.size):
        scale = root_u[:, column][:, None]
        term = tabulated_log_ndtr((shift[:, column][:, None] - scaled_offsets[None, :]) * scale)
        if censored_weights is not None:
            term *= float(censored_weights[column])
        censored_term += term
    joint = (
        base[:, None]
        - effective[:, None] * np.square(offsets)[None, :] / (2.0 * sigma * sigma)
        + censored_term
    )
    marginal = np.logaddexp.reduce(joint, axis=1) + log_measure
    argument = np.argmax(joint, axis=1)
    peak = np.take_along_axis(joint, argument[:, None], axis=1).reshape(-1) + log_measure
    return marginal, peak, offsets[argument], best_level


def shadowing_weights(
    east: np.ndarray, north: np.ndarray, correlation_m: float
) -> np.ndarray | None:
    """Per-measurement weights for spatially correlated shadow fading.

    `w_i = 1 / sum_j exp(-d_ij / correlation_m)`, the sum over every
    measurement including i itself. An isolated stop keeps weight ~1; k
    stops on top of each other each get ~1/k, so together they count as one.
    `None` when correlation is not modelled, which callers treat as the
    original unweighted likelihood.
    """
    if correlation_m <= 0:
        return None
    points = np.column_stack([np.asarray(east, dtype=float), np.asarray(north, dtype=float)])
    deltas = points[:, None, :] - points[None, :, :]
    distances = np.hypot(deltas[..., 0], deltas[..., 1])
    return 1.0 / np.exp(-distances / correlation_m).sum(axis=1)


# Suspect-stop check (Phase 8, Stage 4.2). The Gaussian likelihood absorbs a
# single wild stop by widening sigma -- measured: one stop 25 dB off pushed
# the fitted shadowing from a true 4 dB to 8-10 dB -- so a plain residual
# threshold never fires. A spread taken from the median absolute deviation is
# not inflated by that one stop. Measured on 40 campaigns of 10 stops: a stop
# 25 dB off was named in 35, and clean campaigns raised 4 flags in total.
# A mixture likelihood was also tried; it named the stop as reliably but did
# not locate the transmitter any better and was ~35x slower, so it was not
# adopted. See docs/phase8-fixed-emitter-geolocation.md.
SUSPECT_ROBUST_Z = 3.0
SUSPECT_MIN_DETECTIONS = 5
# Floor on the robust spread: with tightly agreeing stops the MAD can be a
# fraction of a dB, and a stop 2 dB off would read as wildly inconsistent.
SUSPECT_MIN_SPREAD_DB = 2.0


def flag_suspect_stops(residuals: list[dict[str, Any]]) -> dict[str, Any]:
    """Mark detections whose residual is far from the others' by a robust measure.

    Annotates each detection entry in place with `robust_z` and `suspect`, and
    returns a summary. Diagnostic only: nothing is removed or down-weighted.
    """
    detected = [entry for entry in residuals if "residual_db" in entry]
    summary: dict[str, Any] = {
        "method": "median_absolute_deviation",
        "threshold_robust_z": SUSPECT_ROBUST_Z,
        "robust_spread_db": None,
        "labels": [],
    }
    if len(detected) < SUSPECT_MIN_DETECTIONS:
        summary["note"] = (
            f"{len(detected)} detection(s); at least {SUSPECT_MIN_DETECTIONS} are needed before "
            "one stop can be told apart from the rest"
        )
        return summary
    values = np.array([float(entry["residual_db"]) for entry in detected])
    centre = float(np.median(values))
    spread = max(
        1.4826 * float(np.median(np.abs(values - centre))), SUSPECT_MIN_SPREAD_DB
    )
    summary["robust_spread_db"] = round(spread, 3)
    for entry, value in zip(detected, values, strict=True):
        z = (float(value) - centre) / spread
        entry["robust_z"] = round(z, 3)
        entry["suspect"] = abs(z) >= SUSPECT_ROBUST_Z
        if entry["suspect"]:
            summary["labels"].append(entry["label"])
    return summary


def _normalise(log_marginal: np.ndarray) -> np.ndarray:
    finite = log_marginal[np.isfinite(log_marginal)]
    if finite.size == 0:
        return np.full(log_marginal.shape, 1.0 / log_marginal.size)
    shifted = np.exp(log_marginal - float(np.max(finite)))
    shifted[~np.isfinite(shifted)] = 0.0
    total = float(shifted.sum())
    if total <= 0.0:
        return np.full(log_marginal.shape, 1.0 / log_marginal.size)
    return shifted / total


def highest_density_threshold(probability: np.ndarray, mass: float) -> float:
    """Smallest probability value still inside the highest-density region
    holding `mass` of the total probability."""
    flat = np.sort(probability.reshape(-1))[::-1]
    cumulative = np.cumsum(flat)
    index = int(np.searchsorted(cumulative, mass * float(cumulative[-1])))
    index = min(index, flat.size - 1)
    return float(flat[index])


def _refine_extent(
    grid: Grid, probability: np.ndarray, mass: float
) -> tuple[float, float, float, float]:
    threshold = highest_density_threshold(probability, mass)
    mask = probability.reshape(grid.ny, grid.nx) >= threshold
    rows = np.flatnonzero(mask.any(axis=1))
    columns = np.flatnonzero(mask.any(axis=0))
    if rows.size == 0 or columns.size == 0:
        return grid.x_min, grid.x_max, grid.y_min, grid.y_max
    xs, ys = grid.axes()
    pad = grid.resolution_m
    return (
        float(xs[columns[0]] - pad),
        float(xs[columns[-1]] + pad),
        float(ys[rows[0]] - pad),
        float(ys[rows[-1]] + pad),
    )


def solve_site(
    measurements: list[GeoMeasurement], settings: SolveSettings | None = None
) -> SolveResult:
    """Estimate one site's position from its measurements.

    Returns a result in every case. A refusal (`insufficient_evidence`,
    `weak_geometry`, `unbounded_region`) is itself the answer, carrying the
    counts and the reason -- never a confident-looking polygon drawn from
    data that cannot support one.
    """
    resolved = settings or SolveSettings()
    resolved.validate()
    for measurement in measurements:
        measurement.validate()

    detections = [m for m in measurements if m.detected]
    censored = [m for m in measurements if not m.detected]
    warnings: list[str] = []

    if len(detections) < resolved.min_detections:
        return SolveResult(
            status=STATUS_INSUFFICIENT_EVIDENCE,
            status_reason=(
                f"{len(detections)} usable detection(s); at least "
                f"{resolved.min_detections} are required to constrain a position. "
                "A single level cannot separate a powerful transmitter far away from a "
                "weak one nearby, so it says nothing about where the site is"
            ),
            detection_count=len(detections),
            non_detection_count=len(censored),
            warnings=warnings,
        )
    # The unknown reference level absorbs one detection: only the ratios
    # between them locate anything. Non-detections each add a constraint of
    # their own, and counting them here is the point -- the solver has always
    # USED them, while the gate in front of it pretended they did not exist.
    constraints = (len(detections) - 1) + len(censored)
    if constraints < resolved.min_constraint_count:
        return SolveResult(
            status=STATUS_INSUFFICIENT_EVIDENCE,
            status_reason=(
                f"{len(detections)} detection(s) and {len(censored)} non-detection(s) give "
                f"{constraints} independent constraint(s) on a position that needs "
                f"{resolved.min_constraint_count}. Another stop anywhere adds one, whether or "
                "not the site is heard from it"
            ),
            detection_count=len(detections),
            non_detection_count=len(censored),
            warnings=warnings,
        )
    if not censored:
        warnings.append(
            "no non-detections contributed; a region is bounded from the inside only, "
            "so it can extend further than the measurements suggest"
        )

    latitudes = np.array([m.latitude for m in measurements], dtype=float)
    longitudes = np.array([m.longitude for m in measurements], dtype=float)
    projection = LocalProjection.from_points(latitudes, longitudes)
    east, north = projection.to_local(latitudes, longitudes)

    detection_mask = np.array([m.detected for m in measurements], dtype=bool)
    detection_x, detection_y = east[detection_mask], north[detection_mask]
    censored_x, censored_y = east[~detection_mask], north[~detection_mask]
    detection_levels = np.array(
        [float(m.level_db) for m in measurements if m.detected], dtype=float
    )
    censored_levels = np.array(
        [float(m.censor_level_db) for m in measurements if not m.detected], dtype=float
    )
    weights = shadowing_weights(east, north, resolved.shadowing_correlation_m)
    detection_weights = None if weights is None else weights[detection_mask]
    censored_weights = None if weights is None else weights[~detection_mask]
    position_sigma = np.array([float(m.position_sigma_m or 0.0) for m in measurements])
    uncertain = bool(np.any(position_sigma > 0.0))
    detection_position_sigma = position_sigma[detection_mask] if uncertain else None
    censored_position_sigma = position_sigma[~detection_mask] if uncertain else None

    coarse = build_grid(
        float(east.min()) - resolved.margin_m,
        float(east.max()) + resolved.margin_m,
        float(north.min()) - resolved.margin_m,
        float(north.max()) + resolved.margin_m,
        resolved.coarse_resolution_m,
        max_cells=resolved.max_coarse_cells,
    )
    coarse_log, _, _, _, _, _ = _evaluate_grid(
        coarse,
        detection_x=detection_x,
        detection_y=detection_y,
        detection_levels=detection_levels,
        censored_x=censored_x,
        censored_y=censored_y,
        censored_levels=censored_levels,
        settings=resolved,
        detection_weights=detection_weights,
        censored_weights=censored_weights,
        detection_position_sigma=detection_position_sigma,
        censored_position_sigma=censored_position_sigma,
    )
    coarse_probability = _normalise(coarse_log)

    x_min, x_max, y_min, y_max = _refine_extent(coarse, coarse_probability, resolved.refine_mass)
    fine = build_grid(
        x_min,
        x_max,
        y_min,
        y_max,
        resolved.resolution_m,
        max_cells=resolved.max_fine_cells,
        target_cells=resolved.target_fine_cells,
        min_resolution_m=resolved.min_resolution_m,
    )
    fine_log, exponent, sigma_fit, reference_level, best_cell, sigma_log_mass = _evaluate_grid(
        fine,
        detection_x=detection_x,
        detection_y=detection_y,
        detection_levels=detection_levels,
        censored_x=censored_x,
        censored_y=censored_y,
        censored_levels=censored_levels,
        settings=resolved,
        detection_weights=detection_weights,
        censored_weights=censored_weights,
        detection_position_sigma=detection_position_sigma,
        censored_position_sigma=censored_position_sigma,
    )
    probability = _normalise(fine_log)

    xs, ys = fine.axes()
    mode_x = float(xs[best_cell % fine.nx])
    mode_y = float(ys[best_cell // fine.nx])
    mode_latitude, mode_longitude = (
        float(value) for value in projection.to_geographic(mode_x, mode_y)
    )
    grid_x = np.repeat(xs[None, :], fine.ny, axis=0).reshape(-1)
    grid_y = np.repeat(ys[:, None], fine.nx, axis=1).reshape(-1)
    mean_x = float(np.sum(probability * grid_x))
    mean_y = float(np.sum(probability * grid_y))
    mean_latitude, mean_longitude = (
        float(value) for value in projection.to_geographic(mean_x, mean_y)
    )

    residuals: list[dict[str, Any]] = []
    squared_sum = 0.0
    for measurement in measurements:
        m_x, m_y = projection.to_local(measurement.latitude, measurement.longitude)
        distance = max(
            float(math.hypot(float(m_x) - mode_x, float(m_y) - mode_y)),
            resolved.min_distance_m,
        )
        predicted = reference_level - 10.0 * exponent * math.log10(
            distance / resolved.reference_distance_m
        )
        entry: dict[str, Any] = {
            "label": measurement.label,
            "survey_run_id": measurement.survey_run_id,
            "latitude": measurement.latitude,
            "longitude": measurement.longitude,
            "detected": measurement.detected,
            "distance_m": distance,
            "predicted_level_db": predicted,
        }
        if measurement.detected and measurement.level_db is not None:
            residual = float(measurement.level_db) - predicted
            entry["level_db"] = float(measurement.level_db)
            entry["residual_db"] = residual
            squared_sum += residual * residual
        else:
            entry["censor_level_db"] = measurement.censor_level_db
            # A non-detection is consistent whenever the prediction sits
            # below the threshold; only the amount by which it is exceeded
            # is a real disagreement, so that is what gets reported.
            entry["exceedance_db"] = max(0.0, predicted - measurement.censor_level_db)
        residuals.append(entry)
    residual_rms = math.sqrt(squared_sum / len(detections)) if detections else None
    suspects = flag_suspect_stops(residuals)
    if suspects["labels"]:
        warnings.append(
            f"{len(suspects['labels'])} stop(s) disagree with the rest by at least "
            f"{SUSPECT_ROBUST_Z:g} robust standard deviations ({', '.join(suspects['labels'])}); "
            "such a stop may be blocked or have unusual line of sight. Its evidence is still "
            "used -- re-measuring near it is the way to find out"
        )

    bearings = [
        bearing_deg(mode_latitude, mode_longitude, m.latitude, m.longitude) for m in detections
    ]
    span = azimuth_span_deg(bearings)

    # Judged on the coarse pass, which spans the whole requested region.
    # The fine pass is a zoom into where the mass already is, so its edges
    # only mean "the zoom stops here" -- testing them would report every
    # well-constrained site as unbounded.
    edge_mask = coarse_probability.reshape(coarse.ny, coarse.nx) >= highest_density_threshold(
        coarse_probability, max(resolved.credible_levels)
    )
    touches_edge = bool(
        edge_mask[0, :].any()
        or edge_mask[-1, :].any()
        or edge_mask[:, 0].any()
        or edge_mask[:, -1].any()
    )

    status = STATUS_OK
    status_reason = ""
    if touches_edge:
        status = STATUS_UNBOUNDED_REGION
        status_reason = (
            f"the {max(resolved.credible_levels) * 100:.0f}% credible region reaches the edge of "
            "the analysed area; the data does not close it, so the reported area is a lower bound"
        )
    elif span < resolved.min_azimuth_span_deg:
        status = STATUS_WEAK_GEOMETRY
        status_reason = (
            f"detections span only {span:.0f} degrees of azimuth around the mode "
            f"(minimum {resolved.min_azimuth_span_deg:.0f}); from one direction, distance and "
            "transmit power cannot be separated"
        )

    # Too few detections to tell the exponent and the reference level apart.
    # Said before the edge-of-range warning below, because "the exponent is at
    # the edge of the range" invites the reader to widen the range, and when
    # the exponent was never identified in the first place a wider range only
    # lets the fit travel further into a corner.
    fit_status = FIT_IDENTIFIED
    if len(detections) < resolved.min_detections_for_fit:
        fit_status = FIT_UNDERDETERMINED
        warnings.append(
            f"the path-loss exponent and reference level are not identifiable from "
            f"{len(detections)} detection(s): the reference level is fitted per cell and the "
            "exponent chosen from a grid, so two detections are reproduced exactly by many "
            "different pairs. The region below is still honest -- its size is the answer -- but "
            "the fitted numbers are arithmetic rather than measurement and are reported as "
            "unidentifiable"
        )

    if (
        fit_status == FIT_IDENTIFIED
        and exponent in (min(resolved.path_loss_exponents), max(resolved.path_loss_exponents))
        and len(resolved.path_loss_exponents) > 1
    ):
        warnings.append(
            f"the best-fitting path-loss exponent ({exponent:g}) is at the edge of the searched "
            f"range {min(resolved.path_loss_exponents):g}-{max(resolved.path_loss_exponents):g}; "
            "the true value may lie outside it and the region is correspondingly less trustworthy"
        )

    # Which shadow-fading values the evidence actually preferred. Reported
    # rather than hidden: if the weight piles onto the largest sigma on the
    # grid, the data is scattered beyond what the grid can express and the
    # region is optimistic; if it piles onto the smallest, the grid's own
    # floor -- not the data -- is setting the region's size.
    sigma_weights = _normalise(sigma_log_mass)
    sigma_posterior = {
        f"{value:g}": round(float(weight), 4)
        for value, weight in zip(resolved.sigma_db_values, sigma_weights, strict=True)
    }
    sigma_mean = float(np.dot(np.asarray(resolved.sigma_db_values, dtype=float), sigma_weights))
    if len(resolved.sigma_db_values) > 1:
        edge = max(resolved.sigma_db_values)
        if sigma_posterior.get(f"{edge:g}", 0.0) > 0.5:
            warnings.append(
                f"most of the shadow-fading weight sits on the largest value searched "
                f"({edge:g} dB); the measurements scatter at least that much, so the true "
                "spread may be larger still and the region correspondingly optimistic"
            )

    diagnostics = {
        "projection": projection.to_dict(),
        "coarse_grid": coarse.to_dict(),
        "fine_grid": fine.to_dict(),
        "reference_level_offsets": {
            "count": int(reference_level_offsets(len(detections), resolved).size),
            "half_width_db": float(
                reference_level_offsets(len(detections), resolved).max()
            ),
        },
        "shadow_fading": {
            "values_db": list(resolved.sigma_db_values),
            "posterior": sigma_posterior,
            "posterior_mean_db": round(sigma_mean, 3),
            "best_fit_db": sigma_fit,
        },
        "credible_region_touches_grid_edge": touches_edge,
        "suspect_stops": suspects,
        "position_uncertainty": {
            "modelled": uncertain,
            "measurements_with_sigma": int(np.count_nonzero(position_sigma > 0.0)),
            "median_sigma_m": (
                round(float(np.median(position_sigma[position_sigma > 0.0])), 1)
                if uncertain
                else None
            ),
        },
        "shadowing_correlation": {
            "assumed_m": resolved.shadowing_correlation_m,
            # How many independent measurements the evidence was counted as.
            # Equal to the measurement count when correlation is not modelled.
            "effective_measurements": (
                float(len(measurements)) if weights is None else round(float(weights.sum()), 3)
            ),
            "measurements": len(measurements),
        },
        "source_model": SOURCE_MODEL,
    }

    return SolveResult(
        status=status,
        status_reason=status_reason,
        detection_count=len(detections),
        non_detection_count=len(censored),
        warnings=warnings,
        surface=PosteriorSurface(grid=fine, projection=projection, probability=probability),
        mode_latitude=mode_latitude,
        mode_longitude=mode_longitude,
        mean_latitude=mean_latitude,
        mean_longitude=mean_longitude,
        path_loss_exponent=exponent,
        reference_level_db=reference_level,
        residual_rms_db=residual_rms,
        fit_status=fit_status,
        residuals=residuals,
        azimuth_span_deg=span,
        diagnostics=diagnostics,
    )


__all__ = [
    "FIT_IDENTIFIED",
    "FIT_NOT_FITTED",
    "FIT_UNDERDETERMINED",
    "FIT_UNKNOWN",
    "GEOLOCATION_MATURITY",
    "SOURCE_MODEL",
    "STATUS_INSUFFICIENT_EVIDENCE",
    "STATUS_OK",
    "STATUS_UNBOUNDED_REGION",
    "STATUS_WEAK_GEOMETRY",
    "VALIDATION_STATUS",
    "PosteriorSurface",
    "SolveResult",
    "flag_suspect_stops",
    "highest_density_threshold",
    "shadowing_weights",
    "solve_site",
]
