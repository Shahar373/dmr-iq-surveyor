"""Turn a posterior surface into credible-region polygons and GeoJSON.

A credible region here is a *highest-density* region: the smallest set of
cells whose probability sums to the requested mass. Its boundary is an
iso-probability contour, which is what makes "the 90% region" a single
closed shape that can be drawn on a map rather than an ellipse fitted to
something that is often not elliptical at all.

Contour extraction uses matplotlib -- already a dependency of this project
-- through `matplotlib.figure.Figure` rather than `pyplot`, so no global
figure state or interactive backend is involved.
"""

from __future__ import annotations

from typing import Any

import numpy as np
from matplotlib.figure import Figure

from dmr_iq_surveyor.geo.solver import PosteriorSurface, highest_density_threshold


def _ring_signed_area_m2(points: np.ndarray) -> float:
    """Shoelace area in the local metre plane. Positive is counter-clockwise."""
    x = points[:, 0]
    y = points[:, 1]
    return 0.5 * float(np.sum(x * np.roll(y, -1) - np.roll(x, -1) * y))


def _point_in_ring(x: float, y: float, ring: np.ndarray) -> bool:
    """Ray-casting containment test in the local metre plane."""
    inside = False
    count = len(ring)
    for index in range(count):
        x1, y1 = ring[index]
        x2, y2 = ring[(index + 1) % count]
        if (y1 > y) != (y2 > y):
            crossing = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
            if crossing > x:
                inside = not inside
    return inside


def _close_ring(points: np.ndarray) -> np.ndarray:
    if len(points) >= 2 and not np.allclose(points[0], points[-1]):
        return np.vstack([points, points[:1]])
    return points


def _assign_holes(rings: list[np.ndarray]) -> list[list[np.ndarray]]:
    """Group rings into polygons, nesting each ring inside its parent.

    A credible region genuinely can be an annulus -- measurements at a
    similar distance in every direction constrain the range far better than
    the bearing -- so holes are not a theoretical nicety here. Depth is
    counted by containment: even depth is an outer ring, odd depth is a hole
    of the smallest ring that contains it.
    """
    depths: list[int] = []
    containers: list[list[int]] = []
    for index, ring in enumerate(rings):
        probe = ring[0]
        enclosing = [
            other
            for other, candidate in enumerate(rings)
            if other != index and _point_in_ring(float(probe[0]), float(probe[1]), candidate)
        ]
        depths.append(len(enclosing))
        containers.append(enclosing)

    polygons: dict[int, list[np.ndarray]] = {
        index: [rings[index]] for index, depth in enumerate(depths) if depth % 2 == 0
    }
    for index, depth in enumerate(depths):
        if depth % 2 == 0:
            continue
        parents = [other for other in containers[index] if depths[other] == depth - 1]
        if not parents:
            continue
        # Smallest enclosing ring of the right depth is the direct parent.
        parent = min(parents, key=lambda other: abs(_ring_signed_area_m2(rings[other])))
        polygons.setdefault(parent, [rings[parent]]).append(rings[index])
    return [polygons[key] for key in sorted(polygons)]


def credible_regions(
    surface: PosteriorSurface, levels: tuple[float, ...]
) -> list[dict[str, Any]]:
    """Extract one highest-density region per requested probability mass.

    Each region carries the area it actually covers (from the cell count,
    not from the polygon, so a clipped or self-touching contour cannot
    misstate it) and whether it reaches the analysed area's edge.
    """
    grid = surface.grid
    probability = surface.probability.reshape(grid.ny, grid.nx)
    xs, ys = grid.axes()
    cell_area_km2 = (grid.resolution_m / 1000.0) ** 2
    regions: list[dict[str, Any]] = []

    for level in levels:
        threshold = highest_density_threshold(surface.probability, level)
        mask = probability >= threshold
        touches_edge = bool(
            mask[0, :].any() or mask[-1, :].any() or mask[:, 0].any() or mask[:, -1].any()
        )
        rings: list[np.ndarray] = []
        # A degenerate surface (one cell holding everything, or a threshold
        # equal to the maximum) has no contour to trace; the cell-count area
        # below still describes it truthfully.
        if np.isfinite(threshold) and probability.max() > threshold:
            figure = Figure()
            axes = figure.subplots()
            contours = axes.contour(xs, ys, probability, levels=[threshold])
            for segment in contours.allsegs[0]:
                if len(segment) >= 3:
                    rings.append(_close_ring(np.asarray(segment, dtype=float)))

        polygons = _assign_holes(rings) if rings else []
        geographic: list[list[list[list[float]]]] = []
        for polygon in polygons:
            converted: list[list[list[float]]] = []
            for position, ring in enumerate(polygon):
                signed = _ring_signed_area_m2(ring)
                # GeoJSON (RFC 7946) wants exterior rings counter-clockwise
                # and interior rings clockwise.
                wants_counter_clockwise = position == 0
                if (signed < 0) == wants_counter_clockwise:
                    ring = ring[::-1]
                latitudes, longitudes = surface.projection.to_geographic(ring[:, 0], ring[:, 1])
                converted.append(
                    [
                        [round(float(longitude), 7), round(float(latitude), 7)]
                        for longitude, latitude in zip(longitudes, latitudes, strict=True)
                    ]
                )
            geographic.append(converted)

        regions.append(
            {
                "level": float(level),
                "threshold": float(threshold),
                "area_km2": float(mask.sum()) * cell_area_km2,
                "cell_count": int(mask.sum()),
                "touches_analysed_edge": touches_edge,
                "polygons": geographic,
            }
        )
    return regions


def regions_to_geojson(
    regions: list[dict[str, Any]], properties: dict[str, Any] | None = None
) -> dict[str, Any]:
    """One GeoJSON `FeatureCollection`, one MultiPolygon feature per level."""
    base = dict(properties or {})
    features = []
    for region in regions:
        if not region["polygons"]:
            continue
        feature_properties = dict(base)
        feature_properties.update(
            {
                "credible_level": region["level"],
                "area_km2": region["area_km2"],
                "touches_analysed_edge": region["touches_analysed_edge"],
            }
        )
        features.append(
            {
                "type": "Feature",
                "properties": feature_properties,
                "geometry": {"type": "MultiPolygon", "coordinates": region["polygons"]},
            }
        )
    return {"type": "FeatureCollection", "features": features}


__all__ = ["credible_regions", "regions_to_geojson"]
