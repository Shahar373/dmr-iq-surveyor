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
    detection_count: int, settings: SolveSettings
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
    """
    spread = settings.sigma_db / math.sqrt(max(detection_count, 1))
    half_width = (
        settings.reference_level_window_sigma * spread
        + settings.reference_level_window_sigma * settings.sigma_db
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
) -> tuple[np.ndarray, float, float, int]:
    """Marginal log posterior per grid cell, plus the best (n, P0, cell).

    Chunked over cells so peak memory stays bounded no matter how large the
    grid is: the chunk size comes from `max_working_elements` divided by the
    per-cell working set, so one number bounds memory rather than it varying
    with how many non-detections a site happens to have.

    Constants that do not depend on cell, `n` or `P0` are dropped -- the
    Gaussian normaliser and the `K delta^2` offset term -- since they cancel
    in both the marginalisation and the final normalisation.
    """
    xs, ys = grid.axes()
    exponents = np.asarray(settings.path_loss_exponents, dtype=float)
    sigma = float(settings.sigma_db)
    two_sigma_squared = 2.0 * sigma * sigma
    detection_count = int(detection_levels.size)
    censored_count = int(censored_levels.size)
    offsets = reference_level_offsets(detection_count, settings)
    scaled_offsets = offsets / sigma

    # The largest temporary is (cells x offsets): censored measurements are
    # accumulated one at a time rather than broadcast into a three-axis
    # array, which keeps peak memory independent of how many of them a site
    # has, at identical total arithmetic.
    per_cell_elements = max(1, offsets.size)
    chunk = int(
        min(settings.chunk_cells, max(1, settings.max_working_elements // per_cell_elements))
    )

    log_marginal = np.empty(grid.cell_count, dtype=np.float64)
    best_value = -np.inf
    best_exponent = float(exponents[0])
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
        for exponent in exponents:
            # a_i: the reference level that would fit detection i exactly if
            # the transmitter were in this cell with this path-loss exponent.
            fitted = detection_levels[None, :] + 10.0 * exponent * detection_ratio
            best_level = fitted.mean(axis=1)
            residual_sum_squares = np.square(fitted - best_level[:, None]).sum(axis=1)
            base = -residual_sum_squares / two_sigma_squared

            if censored_ratio is None:
                # With no censored terms the offset integral is the same
                # constant for every cell, so the marginal is the base term.
                marginal = base
                peak = base
                peak_offset = np.zeros_like(base)
            else:
                shift = (
                    censored_levels[None, :] + 10.0 * exponent * censored_ratio
                    - best_level[:, None]
                ) / sigma
                censored_term = np.zeros((stop - start, offsets.size), dtype=np.float64)
                for column in range(censored_count):
                    censored_term += tabulated_log_ndtr(
                        shift[:, column][:, None] - scaled_offsets[None, :]
                    )
                joint = (
                    base[:, None]
                    - detection_count * np.square(offsets)[None, :] / two_sigma_squared
                    + censored_term
                )
                marginal = np.logaddexp.reduce(joint, axis=1)
                argument = np.argmax(joint, axis=1)
                peak = np.take_along_axis(joint, argument[:, None], axis=1).reshape(-1)
                peak_offset = offsets[argument]

            np.logaddexp(chunk_accumulator, marginal, out=chunk_accumulator)
            local = int(np.argmax(peak))
            if float(peak[local]) > best_value:
                best_value = float(peak[local])
                best_exponent = float(exponent)
                best_reference = float(best_level[local] + peak_offset[local])
                best_cell = start + local
        log_marginal[start:stop] = chunk_accumulator

    return log_marginal, best_exponent, best_reference, best_cell


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
                f"{resolved.min_detections} are required to constrain a position"
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

    coarse = build_grid(
        float(east.min()) - resolved.margin_m,
        float(east.max()) + resolved.margin_m,
        float(north.min()) - resolved.margin_m,
        float(north.max()) + resolved.margin_m,
        resolved.coarse_resolution_m,
        max_cells=resolved.max_coarse_cells,
    )
    coarse_log, _, _, _ = _evaluate_grid(
        coarse,
        detection_x=detection_x,
        detection_y=detection_y,
        detection_levels=detection_levels,
        censored_x=censored_x,
        censored_y=censored_y,
        censored_levels=censored_levels,
        settings=resolved,
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
    fine_log, exponent, reference_level, best_cell = _evaluate_grid(
        fine,
        detection_x=detection_x,
        detection_y=detection_y,
        detection_levels=detection_levels,
        censored_x=censored_x,
        censored_y=censored_y,
        censored_levels=censored_levels,
        settings=resolved,
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

    if exponent in (min(resolved.path_loss_exponents), max(resolved.path_loss_exponents)) and (
        len(resolved.path_loss_exponents) > 1
    ):
        warnings.append(
            f"the best-fitting path-loss exponent ({exponent:g}) is at the edge of the searched "
            f"range {min(resolved.path_loss_exponents):g}-{max(resolved.path_loss_exponents):g}; "
            "the true value may lie outside it and the region is correspondingly less trustworthy"
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
        "credible_region_touches_grid_edge": touches_edge,
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
        residuals=residuals,
        azimuth_span_deg=span,
        diagnostics=diagnostics,
    )


__all__ = [
    "SOURCE_MODEL",
    "STATUS_INSUFFICIENT_EVIDENCE",
    "STATUS_OK",
    "STATUS_UNBOUNDED_REGION",
    "STATUS_WEAK_GEOMETRY",
    "PosteriorSurface",
    "SolveResult",
    "highest_density_threshold",
    "solve_site",
]
