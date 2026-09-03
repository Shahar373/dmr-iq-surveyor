"""Geometry and the propagation likelihood behind Phase 7 geolocation.

Everything here is deliberately explicit and classical: a log-distance path
loss model, Gaussian shadow fading, and a censored likelihood for
non-detections. No learned classifier is involved, so every number in a
solution can be traced back to a stored measurement and a named parameter
(see `CLAUDE.md`, "no premature ML").

Positions are converted to a local east/north plane anchored at the
measurement centroid. At the scale this project works on -- tens of
kilometres -- an equirectangular projection is accurate to well under a
metre, and it keeps the grid a plain rectangular numpy array.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Any

import numpy as np

# IUGG mean Earth radius. Used for both the local projection and the
# haversine distance so the two agree with each other.
EARTH_RADIUS_M = 6_371_008.8


@dataclass(slots=True, frozen=True)
class LocalProjection:
    """Equirectangular projection about a reference latitude/longitude."""

    latitude0: float
    longitude0: float

    @classmethod
    def from_points(cls, latitudes: np.ndarray, longitudes: np.ndarray) -> LocalProjection:
        return cls(float(np.mean(latitudes)), float(np.mean(longitudes)))

    @property
    def _cos_lat0(self) -> float:
        return math.cos(math.radians(self.latitude0))

    def to_local(
        self, latitudes: np.ndarray | float, longitudes: np.ndarray | float
    ) -> tuple[np.ndarray, np.ndarray]:
        """(latitude, longitude) -> (east metres, north metres)."""
        east = np.radians(np.asarray(longitudes, dtype=float) - self.longitude0) * (
            EARTH_RADIUS_M * self._cos_lat0
        )
        north = np.radians(np.asarray(latitudes, dtype=float) - self.latitude0) * EARTH_RADIUS_M
        return east, north

    def to_geographic(
        self, east_m: np.ndarray | float, north_m: np.ndarray | float
    ) -> tuple[np.ndarray, np.ndarray]:
        """(east metres, north metres) -> (latitude, longitude)."""
        latitude = self.latitude0 + np.degrees(np.asarray(north_m, dtype=float) / EARTH_RADIUS_M)
        longitude = self.longitude0 + np.degrees(
            np.asarray(east_m, dtype=float) / (EARTH_RADIUS_M * self._cos_lat0)
        )
        return latitude, longitude

    def to_dict(self) -> dict[str, float]:
        return asdict(self)


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = phi2 - phi1
    d_lambda = math.radians(lon2 - lon1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Initial great-circle bearing from point 1 to point 2, in [0, 360)."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_lambda = math.radians(lon2 - lon1)
    y = math.sin(d_lambda) * math.cos(phi2)
    x = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(d_lambda)
    return math.degrees(math.atan2(y, x)) % 360.0


def azimuth_span_deg(bearings: list[float]) -> float:
    """Angular coverage of a set of bearings: 360 minus the largest gap.

    Two measurements 5 degrees apart give a span near 0 however far apart
    they are on the ground; measurements surrounding a point give a span
    near 360. This is the geometry quality that decides whether position
    and transmit power can be separated at all -- from one direction they
    trade against each other and the posterior runs away down a corridor.
    """
    if len(bearings) < 2:
        return 0.0
    ordered = sorted(angle % 360.0 for angle in bearings)
    gaps = [
        (ordered[(index + 1) % len(ordered)] - ordered[index]) % 360.0
        for index in range(len(ordered))
    ]
    largest = max(gaps)
    # All bearings identical: every gap is 0 except the wrap-around, which
    # is also 0 under the modulo. Treat that as no coverage at all.
    if largest == 0.0:
        return 0.0
    return float(360.0 - largest)


@dataclass(slots=True)
class GeoMeasurement:
    """One site-level measurement at one place.

    `level_db` is meaningful only when `detected`; otherwise all that is
    known is that the level stayed below `censor_level_db`, which is the
    run's own detection threshold in the same units.
    """

    label: str
    latitude: float
    longitude: float
    detected: bool
    level_db: float | None
    censor_level_db: float
    survey_run_id: str = ""
    frequency_hz: float = 0.0

    def validate(self) -> None:
        if not -90.0 <= self.latitude <= 90.0:
            raise ValueError(f"{self.label}: latitude out of range: {self.latitude}")
        if not -180.0 <= self.longitude <= 180.0:
            raise ValueError(f"{self.label}: longitude out of range: {self.longitude}")
        if self.detected and self.level_db is None:
            raise ValueError(f"{self.label}: a detected measurement must carry a level")


@dataclass(slots=True)
class SolveSettings:
    """Every knob of the estimator, stored with each solution.

    Defaults are ordinary suburban 800 MHz values, not measurements. None
    of them is claimed to be calibrated for this deployment; they are
    written into `geo_solutions.settings_json` so a result can always be
    read against the assumptions that produced it.
    """

    # Shadow fading standard deviation. 8 dB is a common suburban value at
    # UHF; wider absorbs outliers at the cost of a larger credible region.
    # Used directly wherever a single representative value is needed (the
    # `P0` window, the planner's detection probability); the solver itself
    # marginalises over `sigma_db_values` instead.
    sigma_db: float = 8.0
    # Shadow fading values marginalised over, for the same reason the
    # path-loss exponent is: it is not known, and assuming one value states
    # a confidence the data does not support. Measured on synthetic data,
    # the assumed sigma changes the 90% region area by nearly three orders
    # of magnitude, which makes it the single most influential number here.
    #
    # The range is deliberately bounded from BELOW by physics rather than
    # left to the data. Residual scatter systematically understates sigma
    # when the fit has few degrees of freedom -- data generated with 6 dB of
    # shadowing fitted to 3.8 dB of residual over five stops -- and solving
    # at that understated value produced an 8 km2 region whose centre was
    # 2.8 km from the true transmitter: small, and wrong. Real outdoor
    # shadowing is not below a few dB, so the grid does not offer values
    # that would let a lucky fit claim near-certainty.
    sigma_db_values: tuple[float, ...] = (4.0, 6.0, 8.0, 10.0, 12.0)
    # Path-loss exponents marginalised over. Deliberately a range, never a
    # single universal value -- see docs/TRANSMITTER-LOCATION-STUDY.md.
    path_loss_exponents: tuple[float, ...] = (2.0, 2.5, 3.0, 3.5, 4.0, 4.5, 5.0)
    reference_distance_m: float = 1000.0
    # Distances below this are clamped: the model diverges at zero, and no
    # measurement is meaningfully "at" the transmitter.
    min_distance_m: float = 50.0
    resolution_m: float = 100.0
    coarse_resolution_m: float = 500.0
    min_resolution_m: float = 25.0
    margin_m: float = 25_000.0
    max_coarse_cells: int = 120_000
    max_fine_cells: int = 120_000
    target_fine_cells: int = 20_000
    # Fraction of coarse posterior mass whose bounding box the fine pass
    # refines. Deliberately close to 1: cutting to 0.9 here would crop the
    # tail that decides whether a region is bounded at all.
    refine_mass: float = 0.999
    # `P0` is integrated over offsets around each cell's own best fit
    # (see solver.reference_level_offsets), so these describe a window
    # width in standard deviations and how densely to sample it.
    reference_level_samples: int = 25
    reference_level_window_sigma: float = 3.0
    credible_levels: tuple[float, ...] = (0.5, 0.9)
    # Two detections are the floor for any answer at all: one level alone
    # cannot separate transmit power from distance, so a single detection
    # constrains nothing about position.
    min_detections: int = 2
    # Independent constraints required, counted as `(detections - 1) +
    # non_detections`. Detections contribute one fewer than their count
    # because the unknown reference level absorbs one of them -- only the
    # RATIOS between detections carry position information. Non-detections
    # each contribute a constraint of their own ("it is not within this
    # radius of here"), which is why they are counted here at full weight.
    #
    # This replaces a flat minimum on detections alone, which discarded
    # genuinely good answers: two detections with four non-detections
    # solved to a bounded 19 km2 region at 173 degrees of azimuth span,
    # and was being refused outright. Answers that really are poor are not
    # let through silently -- `unbounded_region` and `weak_geometry` label
    # them, which is what those statuses are for.
    min_constraint_count: int = 2
    min_azimuth_span_deg: float = 90.0
    # Grid cells processed per chunk. Bounds peak memory independently of
    # the grid size, the same discipline the IQ stages use for sample data.
    chunk_cells: int = 65_536
    # Upper bound on the elements of the largest temporary array the
    # solver builds (cells x non-detections x reference levels). The
    # cell chunk is derived from this, so peak memory is set by one
    # number rather than varying with how many non-detections a site has.
    max_working_elements: int = 4_000_000

    def validate(self) -> None:
        if self.sigma_db <= 0:
            raise ValueError("sigma_db must be positive")
        if not self.path_loss_exponents:
            raise ValueError("at least one path-loss exponent is required")
        if any(exponent <= 0 for exponent in self.path_loss_exponents):
            raise ValueError("path-loss exponents must be positive")
        if self.reference_distance_m <= 0:
            raise ValueError("reference_distance_m must be positive")
        if self.min_distance_m <= 0:
            raise ValueError("min_distance_m must be positive")
        if self.resolution_m <= 0 or self.coarse_resolution_m <= 0:
            raise ValueError("grid resolutions must be positive")
        if self.min_resolution_m <= 0:
            raise ValueError("min_resolution_m must be positive")
        if self.max_coarse_cells < 1 or self.max_fine_cells < 1:
            raise ValueError("grid cell limits must be positive")
        if self.coarse_resolution_m < self.resolution_m:
            raise ValueError("coarse_resolution_m must not be finer than resolution_m")
        if self.margin_m < 0:
            raise ValueError("margin_m must be non-negative")
        if self.reference_level_samples < 3:
            raise ValueError("reference_level_samples must be at least 3")
        if self.reference_level_window_sigma <= 0:
            raise ValueError("reference_level_window_sigma must be positive")
        if not 0.0 < self.refine_mass <= 1.0:
            raise ValueError("refine_mass must be in (0, 1]")
        if any(not 0.0 < level < 1.0 for level in self.credible_levels):
            raise ValueError("credible_levels must be strictly between 0 and 1")
        if self.min_detections < 1:
            raise ValueError("min_detections must be at least 1")
        if self.min_constraint_count < 1:
            raise ValueError("min_constraint_count must be at least 1")
        if not self.sigma_db_values:
            raise ValueError("at least one shadow-fading value is required")
        if any(value <= 0 for value in self.sigma_db_values):
            raise ValueError("shadow-fading values must be positive")
        if self.chunk_cells < 1:
            raise ValueError("chunk_cells must be at least 1")
        if self.max_working_elements < 1:
            raise ValueError("max_working_elements must be at least 1")

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["path_loss_exponents"] = list(self.path_loss_exponents)
        payload["credible_levels"] = list(self.credible_levels)
        payload["sigma_db_values"] = list(self.sigma_db_values)
        return payload


@dataclass(slots=True)
class Grid:
    """A rectangular local-plane grid, in metres east/north."""

    x_min: float
    y_min: float
    resolution_m: float
    nx: int
    ny: int

    @property
    def cell_count(self) -> int:
        return self.nx * self.ny

    @property
    def x_max(self) -> float:
        return self.x_min + (self.nx - 1) * self.resolution_m

    @property
    def y_max(self) -> float:
        return self.y_min + (self.ny - 1) * self.resolution_m

    def axes(self) -> tuple[np.ndarray, np.ndarray]:
        xs = self.x_min + np.arange(self.nx, dtype=float) * self.resolution_m
        ys = self.y_min + np.arange(self.ny, dtype=float) * self.resolution_m
        return xs, ys

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["cell_count"] = self.cell_count
        return payload


def build_grid(
    x_min: float,
    x_max: float,
    y_min: float,
    y_max: float,
    resolution_m: float,
    *,
    max_cells: int,
    target_cells: int | None = None,
    min_resolution_m: float = 10.0,
) -> Grid:
    """Build a grid covering the requested extent, adapting the resolution.

    The extent is never cropped -- only the resolution moves. Coarsening a
    diffuse region is honest ("we resolved it to 400 m"); analysing a
    smaller area than asked for and not saying so is not.

    The resolution is halved or doubled from `resolution_m` so that the cell
    count stays under `max_cells` and, where the extent is small enough to
    allow it, reaches at least `target_cells`. A credible region two
    kilometres across deserves finer cells than the default, and one sixty
    kilometres across gains nothing from them.
    """
    if x_max < x_min or y_max < y_min:
        raise ValueError("grid extent is inverted")
    if min_resolution_m <= 0:
        raise ValueError("min_resolution_m must be positive")
    span_x = max(x_max - x_min, resolution_m)
    span_y = max(y_max - y_min, resolution_m)

    def cell_count(step: float) -> int:
        return (math.floor(span_x / step) + 1) * (math.floor(span_y / step) + 1)

    effective = float(resolution_m)
    while cell_count(effective) > max_cells:
        effective *= 2.0
    if target_cells is not None:
        while (
            effective / 2.0 >= min_resolution_m
            and cell_count(effective) < target_cells
            and cell_count(effective / 2.0) <= max_cells
        ):
            effective /= 2.0
    nx = math.floor(span_x / effective) + 1
    ny = math.floor(span_y / effective) + 1
    return Grid(x_min=x_min, y_min=y_min, resolution_m=effective, nx=nx, ny=ny)


__all__ = [
    "EARTH_RADIUS_M",
    "GeoMeasurement",
    "Grid",
    "LocalProjection",
    "SolveSettings",
    "azimuth_span_deg",
    "bearing_deg",
    "build_grid",
    "haversine_m",
]
