"""Where the next stop is worth making.

Geometry decides a campaign more than stop count does, and nothing in the
system helped the operator choose. This does, from what is already known.

The measure is the one an experimenter would use: a stop is valuable where
its outcome is least predictable. For a binary observation -- the site is
heard, or it is not -- the information it can carry is the binary entropy of
its predicted probability, maximised at exactly 50/50. A place where a site
is certainly heard, or certainly not, teaches nothing about where that site
is; a place where the current posterior genuinely cannot say teaches the most.

For site `s` and candidate place `x`, using that site's own fitted reference
level and path-loss exponent:

    p_detect(x) = sum over posterior cells  P(cell) * Phi( (mu(cell, x) - y_threshold) / sigma )
    value_s(x)  = H_binary( p_detect(x) )

summed over sites, weighted so a site whose region is already tight stops
pulling the plan, and damped near places already measured, since a second
stop beside the first repeats a measurement rather than adding one.

This is a planning aid computed from the current posteriors, not a
prediction about the transmitters. It is explainable in one line per term and
uses no learned model.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Any

import numpy as np

from dmr_iq_surveyor.geo.model import LocalProjection, SolveSettings
from dmr_iq_surveyor.geo.solver import tabulated_log_ndtr


@dataclass(slots=True)
class PlanSettings:
    candidate_spacing_m: float = 1000.0
    margin_m: float = 10_000.0
    max_candidates: int = 4_000
    # The posterior is summarised by its strongest cells, which carry
    # essentially all of its mass, so the plan stays fast on a Pi.
    posterior_cells: int = 1_500
    # A stop this close to an existing one mostly repeats it.
    novelty_scale_m: float = 700.0
    # How much of a candidate's value is modulated by the angle it adds.
    #
    # Entropy alone answers "is the outcome here uncertain?", which is
    # satisfied all the way around a ring at the edge of a site's audible
    # range -- including the part of that ring the operator has already
    # driven to. It cannot distinguish a stop that opens the azimuth span
    # from one that repeats an existing bearing further out, and azimuth
    # span is precisely what decides whether a region closes: three stops
    # spanning 14 degrees produced a 2,513 km2 region from clean data.
    #
    # 0 restores pure entropy; 1 makes a candidate on an existing bearing
    # worthless however uncertain it is. The default keeps entropy in
    # charge and lets angle break its many near-ties.
    azimuth_weight: float = 0.6
    # A site whose 90% region is already smaller than this has little left to
    # gain and stops dominating the plan.
    satisfied_area_km2: float = 3.0
    top_n: int = 5
    # Whether sites whose propagation fit was never identified may steer the
    # plan. Off by default: such a site's reference level and exponent are
    # reproduced exactly by many different pairs, so the detection probability
    # the planner predicts from them at a candidate stop is arithmetic, not a
    # forecast. The sites are still solved and still reported -- they simply
    # do not get a vote on where to drive until they have enough detections to
    # earn one. Set it true to restore the old behaviour.
    plan_from_underdetermined_fits: bool = False
    chunk_candidates: int = 512

    def validate(self) -> None:
        if self.candidate_spacing_m <= 0:
            raise ValueError("candidate_spacing_m must be positive")
        if self.max_candidates < 1:
            raise ValueError("max_candidates must be at least 1")
        if self.posterior_cells < 1:
            raise ValueError("posterior_cells must be at least 1")
        if self.top_n < 1:
            raise ValueError("top_n must be at least 1")
        if not 0.0 <= self.azimuth_weight <= 1.0:
            raise ValueError("azimuth_weight must be between 0 and 1")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class SiteTarget:
    """One site's posterior, reduced to what the plan needs."""

    site_key: str
    east_m: np.ndarray
    north_m: np.ndarray
    weight_cell: np.ndarray
    reference_level_db: float
    path_loss_exponent: float
    threshold_db: float
    weight: float


def _binary_entropy(probability: np.ndarray) -> np.ndarray:
    clipped = np.clip(probability, 1e-12, 1.0 - 1e-12)
    return -(clipped * np.log2(clipped) + (1.0 - clipped) * np.log2(1.0 - clipped))


def build_target(
    *,
    site_key: str,
    surface: Any,
    projection: LocalProjection,
    reference_level_db: float,
    path_loss_exponent: float,
    threshold_db: float,
    area_km2: float | None,
    settings: PlanSettings,
) -> SiteTarget | None:
    """Reduce one solved site to the cells that carry its posterior mass."""
    probability = np.asarray(surface.probability, dtype=float)
    if probability.size == 0 or not np.isfinite(probability).any():
        return None
    keep = min(settings.posterior_cells, probability.size)
    indices = np.argpartition(probability, probability.size - keep)[-keep:]
    weights = probability[indices]
    total = float(weights.sum())
    if total <= 0.0:
        return None
    weights = weights / total

    grid = surface.grid
    xs, ys = grid.axes()
    east = xs[indices % grid.nx]
    north = ys[indices // grid.nx]

    # A site already pinned down has little left to give, and should not keep
    # dragging the operator back to the same place.
    if area_km2 is None:
        weight = 1.0
    else:
        weight = float(np.clip(area_km2 / settings.satisfied_area_km2, 0.0, 1.0))
    if weight <= 0.0:
        return None

    return SiteTarget(
        site_key=site_key,
        east_m=east,
        north_m=north,
        weight_cell=weights,
        reference_level_db=reference_level_db,
        path_loss_exponent=path_loss_exponent,
        threshold_db=threshold_db,
        weight=weight,
    )


def _detection_probability(
    target: SiteTarget,
    candidate_east: np.ndarray,
    candidate_north: np.ndarray,
    solve: SolveSettings,
) -> np.ndarray:
    """P(this site is detected at each candidate), under its own posterior."""
    distance = np.hypot(
        candidate_east[:, None] - target.east_m[None, :],
        candidate_north[:, None] - target.north_m[None, :],
    )
    np.maximum(distance, solve.min_distance_m, out=distance)
    predicted = target.reference_level_db - 10.0 * target.path_loss_exponent * np.log10(
        distance / solve.reference_distance_m
    )
    # P(level >= threshold) = Phi((predicted - threshold) / sigma)
    detect = np.exp(tabulated_log_ndtr((predicted - target.threshold_db) / solve.sigma_db))
    return detect @ target.weight_cell


def _azimuth_opening(
    target: SiteTarget,
    candidate_east: np.ndarray,
    candidate_north: np.ndarray,
    visited_east: np.ndarray,
    visited_north: np.ndarray,
) -> np.ndarray:
    """How much new bearing each candidate adds around this site, in 0..1.

    Measured as the angular distance from a candidate's bearing (seen from
    the site's posterior centroid) to the NEAREST bearing already observed.
    A candidate on top of an existing bearing scores 0 however far away it
    is; one on the opposite side of the site scores 1.

    Every visited stop counts, not only the ones that heard the site: a
    non-detection from a fresh bearing bounds the site from that side, which
    is the same geometric service a detection performs.
    """
    if visited_east.size == 0:
        return np.ones(candidate_east.size, dtype=float)
    centre_east = float(target.east_m @ target.weight_cell)
    centre_north = float(target.north_m @ target.weight_cell)
    observed = np.arctan2(visited_east - centre_east, visited_north - centre_north)
    candidate = np.arctan2(candidate_east - centre_east, candidate_north - centre_north)
    separation = np.abs(candidate[:, None] - observed[None, :])
    np.minimum(separation, 2.0 * math.pi - separation, out=separation)
    return separation.min(axis=1) / math.pi


def plan_next_stops(
    *,
    targets: list[SiteTarget],
    projection: LocalProjection,
    visited: list[tuple[float, float]],
    solve: SolveSettings,
    settings: PlanSettings | None = None,
) -> dict[str, Any]:
    """Rank candidate places by how much a stop there would teach."""
    resolved = settings or PlanSettings()
    resolved.validate()
    if not targets:
        return {
            "status": "no_targets",
            "reason": (
                "no site has a posterior to plan against yet; make a few stops spread around the "
                "area first"
            ),
            "candidates": [],
            "top_stops": [],
            "settings": resolved.to_dict(),
        }

    visited_east: list[float] = []
    visited_north: list[float] = []
    for latitude, longitude in visited:
        east, north = projection.to_local(latitude, longitude)
        visited_east.append(float(east))
        visited_north.append(float(north))

    # The search area covers everywhere a site might be, plus a margin, so a
    # stop that would bound a site from the far side is reachable by the plan.
    all_east = np.concatenate([target.east_m for target in targets] + [np.asarray(visited_east or [0.0])])
    all_north = np.concatenate([target.north_m for target in targets] + [np.asarray(visited_north or [0.0])])
    spacing = resolved.candidate_spacing_m
    x_min, x_max = float(all_east.min()) - resolved.margin_m, float(all_east.max()) + resolved.margin_m
    y_min, y_max = float(all_north.min()) - resolved.margin_m, float(all_north.max()) + resolved.margin_m
    while ((x_max - x_min) / spacing + 1) * ((y_max - y_min) / spacing + 1) > resolved.max_candidates:
        spacing *= 1.5
    xs = np.arange(x_min, x_max + spacing, spacing)
    ys = np.arange(y_min, y_max + spacing, spacing)
    mesh_x, mesh_y = np.meshgrid(xs, ys)
    candidate_east = mesh_x.reshape(-1)
    candidate_north = mesh_y.reshape(-1)

    visited_east_array = np.asarray(visited_east, dtype=float)
    visited_north_array = np.asarray(visited_north, dtype=float)

    value = np.zeros(candidate_east.size, dtype=float)
    per_site: dict[str, np.ndarray] = {}
    for target in targets:
        site_value = np.empty(candidate_east.size, dtype=float)
        for start in range(0, candidate_east.size, resolved.chunk_candidates):
            stop = min(start + resolved.chunk_candidates, candidate_east.size)
            probability = _detection_probability(
                target, candidate_east[start:stop], candidate_north[start:stop], solve
            )
            site_value[start:stop] = _binary_entropy(probability)
        # Angle modulates entropy rather than replacing it: an outcome that
        # is already certain teaches nothing from any bearing, so a stop
        # there is not worth making however much geometry it would add.
        if resolved.azimuth_weight > 0.0:
            opening = _azimuth_opening(
                target, candidate_east, candidate_north, visited_east_array, visited_north_array
            )
            site_value = site_value * (
                (1.0 - resolved.azimuth_weight) + resolved.azimuth_weight * opening
            )
        per_site[target.site_key] = site_value
        value += target.weight * site_value

    # A stop beside an existing one repeats a measurement rather than adding
    # one, so proximity to somewhere already measured damps the value.
    if visited_east:
        nearest = np.min(
            np.hypot(
                candidate_east[:, None] - np.asarray(visited_east)[None, :],
                candidate_north[:, None] - np.asarray(visited_north)[None, :],
            ),
            axis=1,
        )
        novelty = 1.0 - np.exp(-nearest / resolved.novelty_scale_m)
    else:
        novelty = np.ones_like(value)
    value = value * novelty

    peak = float(value.max()) if value.size else 0.0
    normalised = value / peak if peak > 0 else value

    order = np.argsort(value)[::-1]
    top: list[dict[str, Any]] = []
    chosen_east: list[float] = []
    chosen_north: list[float] = []
    for index in order:
        if len(top) >= resolved.top_n:
            break
        east, north = float(candidate_east[index]), float(candidate_north[index])
        # Spread the recommendations: three adjacent cells of one hot spot are
        # one suggestion, not three.
        if any(
            math.hypot(east - other_east, north - other_north) < max(spacing * 2.0, 1500.0)
            for other_east, other_north in zip(chosen_east, chosen_north, strict=True)
        ):
            continue
        chosen_east.append(east)
        chosen_north.append(north)
        latitude, longitude = projection.to_geographic(east, north)
        helps = sorted(
            ((per_site[target.site_key][index] * target.weight, target.site_key) for target in targets),
            reverse=True,
        )[:3]
        top.append(
            {
                "latitude": round(float(latitude), 6),
                "longitude": round(float(longitude), 6),
                "value": round(float(normalised[index]), 4),
                "helps_most": [
                    {"site_key": site_key, "value": round(float(score), 4)}
                    for score, site_key in helps
                    if score > 0
                ],
            }
        )

    latitudes, longitudes = projection.to_geographic(candidate_east, candidate_north)
    keep = normalised >= 0.35
    candidates = [
        {
            "latitude": round(float(latitudes[index]), 6),
            "longitude": round(float(longitudes[index]), 6),
            "value": round(float(normalised[index]), 3),
        }
        for index in np.flatnonzero(keep)
    ]
    return {
        "status": "ok" if top else "no_useful_candidate",
        "reason": ""
        if top
        else "every candidate place has a predictable outcome; more stops of this kind will not help",
        "spacing_m": spacing,
        "candidate_count": int(candidate_east.size),
        "candidates": candidates,
        "top_stops": top,
        "sites_considered": [target.site_key for target in targets],
        "settings": resolved.to_dict(),
    }


def plan_to_geojson(plan: dict[str, Any]) -> dict[str, Any]:
    """The plan as map layers: a value field, and the ranked suggestions."""
    features: list[dict[str, Any]] = []
    half = plan.get("spacing_m", 1000.0) / 2.0
    for candidate in plan.get("candidates", []):
        # Degrees per metre, good enough for a planning cell at this latitude.
        d_lat = half / 110_574.0
        d_lon = half / (111_320.0 * math.cos(math.radians(candidate["latitude"])))
        latitude, longitude = candidate["latitude"], candidate["longitude"]
        features.append(
            {
                "type": "Feature",
                "properties": {"kind": "plan_cell", "value": candidate["value"]},
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [
                        [
                            [longitude - d_lon, latitude - d_lat],
                            [longitude + d_lon, latitude - d_lat],
                            [longitude + d_lon, latitude + d_lat],
                            [longitude - d_lon, latitude + d_lat],
                            [longitude - d_lon, latitude - d_lat],
                        ]
                    ],
                },
            }
        )
    for rank, stop in enumerate(plan.get("top_stops", []), start=1):
        features.append(
            {
                "type": "Feature",
                "properties": {
                    "kind": "plan_stop",
                    "rank": rank,
                    "value": stop["value"],
                    "helps_most": stop["helps_most"],
                },
                "geometry": {"type": "Point", "coordinates": [stop["longitude"], stop["latitude"]]},
            }
        )
    return {"type": "FeatureCollection", "features": features}


__all__ = [
    "PlanSettings",
    "SiteTarget",
    "build_target",
    "plan_next_stops",
    "plan_to_geojson",
]
