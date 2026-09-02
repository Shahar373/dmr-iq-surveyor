"""Geometry, the grid posterior, and the contours drawn from it."""

from __future__ import annotations

import math

import numpy as np
import pytest
from fixtures.geo_scenario import fast_solve_settings
from scipy.special import log_ndtr

from dmr_iq_surveyor.geo.contours import credible_regions, regions_to_geojson
from dmr_iq_surveyor.geo.model import (
    GeoMeasurement,
    Grid,
    LocalProjection,
    SolveSettings,
    azimuth_span_deg,
    bearing_deg,
    build_grid,
    haversine_m,
)
from dmr_iq_surveyor.geo.solver import (
    STATUS_INSUFFICIENT_EVIDENCE,
    STATUS_OK,
    STATUS_UNBOUNDED_REGION,
    STATUS_WEAK_GEOMETRY,
    PosteriorSurface,
    highest_density_threshold,
    reference_level_offsets,
    solve_site,
    tabulated_log_ndtr,
)

TRUE_LATITUDE = 32.050
TRUE_LONGITUDE = 34.800

# Ring of stops around the transmitter, plus a distant arc that will not
# hear it -- the geometry a real multi-stop campaign aims for.
NEAR_STOPS = [(32.045, 34.795), (32.056, 34.806), (32.041, 34.809), (32.059, 34.791)]
MID_STOPS = [(32.020, 34.760), (32.085, 34.770), (32.075, 34.855), (32.015, 34.850)]
FAR_STOPS = [(31.950, 34.700), (32.150, 34.700), (32.150, 34.920), (31.950, 34.920)]


def _measurements(
    stops: list[tuple[float, float]],
    *,
    reference_level_db: float = 25.0,
    exponent: float = 3.4,
    threshold_db: float = 4.0,
    noise_db: float = 0.0,
    seed: int = 0,
) -> list[GeoMeasurement]:
    generator = np.random.default_rng(seed)
    measurements = []
    for index, (latitude, longitude) in enumerate(stops):
        distance = max(haversine_m(latitude, longitude, TRUE_LATITUDE, TRUE_LONGITUDE), 50.0)
        level = reference_level_db - 10.0 * exponent * math.log10(distance / 1000.0)
        if noise_db:
            level += float(generator.normal(0.0, noise_db))
        detected = level >= threshold_db
        measurements.append(
            GeoMeasurement(
                label=f"stop{index}",
                latitude=latitude,
                longitude=longitude,
                detected=detected,
                level_db=level if detected else None,
                censor_level_db=threshold_db,
            )
        )
    return measurements


# ------------------------------------------------------------- geometry


def test_local_projection_round_trips() -> None:
    projection = LocalProjection(32.0, 34.8)
    east, north = projection.to_local(np.array([32.07]), np.array([34.86]))
    latitude, longitude = projection.to_geographic(east, north)
    assert float(latitude[0]) == pytest.approx(32.07, abs=1e-9)
    assert float(longitude[0]) == pytest.approx(34.86, abs=1e-9)


def test_local_plane_distance_matches_haversine() -> None:
    projection = LocalProjection(32.0, 34.8)
    east, north = projection.to_local(np.array([32.05]), np.array([34.85]))
    planar = math.hypot(float(east[0]), float(north[0]))
    assert planar == pytest.approx(haversine_m(32.0, 34.8, 32.05, 34.85), rel=2e-4)


def test_bearing_cardinal_directions() -> None:
    assert bearing_deg(32.0, 34.8, 32.1, 34.8) == pytest.approx(0.0, abs=0.01)
    assert bearing_deg(32.0, 34.8, 32.0, 34.9) == pytest.approx(90.0, abs=0.05)
    assert bearing_deg(32.0, 34.8, 31.9, 34.8) == pytest.approx(180.0, abs=0.01)


def test_azimuth_span_rewards_surrounding_geometry() -> None:
    assert azimuth_span_deg([0.0, 90.0, 180.0, 270.0]) == pytest.approx(270.0)
    assert azimuth_span_deg([10.0, 20.0, 30.0]) == pytest.approx(20.0)
    assert azimuth_span_deg([5.0]) == 0.0
    assert azimuth_span_deg([5.0, 5.0]) == 0.0


def test_build_grid_coarsens_rather_than_cropping() -> None:
    grid = build_grid(-50_000, 50_000, -50_000, 50_000, 100.0, max_cells=20_000)
    assert grid.cell_count <= 20_000
    assert grid.resolution_m > 100.0
    # The requested extent is still covered end to end.
    assert grid.x_max >= 49_000 and grid.y_max >= 49_000


def test_build_grid_refines_a_small_extent_towards_the_target() -> None:
    coarse = build_grid(-1_000, 1_000, -1_000, 1_000, 100.0, max_cells=20_000)
    refined = build_grid(
        -1_000, 1_000, -1_000, 1_000, 100.0, max_cells=20_000, target_cells=6_000, min_resolution_m=25.0
    )
    assert refined.resolution_m < coarse.resolution_m
    assert refined.resolution_m >= 25.0


def test_build_grid_rejects_inverted_extent() -> None:
    with pytest.raises(ValueError, match="inverted"):
        build_grid(10.0, 0.0, 0.0, 10.0, 100.0, max_cells=1000)


# --------------------------------------------------------------- numerics


def test_tabulated_log_ndtr_matches_scipy() -> None:
    z = np.linspace(-45.0, 9.0, 50_000)
    assert float(np.max(np.abs(tabulated_log_ndtr(z.copy()) - log_ndtr(z)))) < 1e-5


def test_tabulated_log_ndtr_stays_finite_far_below_the_table() -> None:
    values = tabulated_log_ndtr(np.array([-1e6, -100.0, 0.0, 50.0]))
    assert np.all(np.isfinite(values))
    assert values[-1] == pytest.approx(0.0, abs=1e-6)


def test_reference_level_offsets_narrow_as_detections_accumulate() -> None:
    settings = SolveSettings()
    few = reference_level_offsets(2, settings)
    many = reference_level_offsets(50, settings)
    assert float(many.max()) < float(few.max())
    assert few.size == many.size == settings.reference_level_samples


def test_settings_validation_rejects_nonsense() -> None:
    with pytest.raises(ValueError, match="sigma_db"):
        SolveSettings(sigma_db=0.0).validate()
    with pytest.raises(ValueError, match="credible_levels"):
        SolveSettings(credible_levels=(1.5,)).validate()
    with pytest.raises(ValueError, match="coarse_resolution_m"):
        SolveSettings(coarse_resolution_m=10.0, resolution_m=100.0).validate()


# ---------------------------------------------------------------- solving


def test_recovers_a_known_transmitter() -> None:
    result = solve_site(
        _measurements(NEAR_STOPS + MID_STOPS + FAR_STOPS), fast_solve_settings()
    )
    assert result.status == STATUS_OK
    error_m = haversine_m(
        result.mode_latitude, result.mode_longitude, TRUE_LATITUDE, TRUE_LONGITUDE
    )
    assert error_m < 1500.0, f"mode was {error_m:.0f} m from the true position"
    assert result.detection_count >= 3
    assert result.non_detection_count >= 1


def test_the_region_shrinks_as_sessions_accumulate() -> None:
    settings = fast_solve_settings()

    def area(measurements: list[GeoMeasurement]) -> float:
        result = solve_site(measurements, settings)
        assert result.surface is not None
        return credible_regions(result.surface, (0.9,))[0]["area_km2"]

    one = _measurements(NEAR_STOPS + MID_STOPS + FAR_STOPS, noise_db=3.0, seed=1)
    two = one + _measurements(NEAR_STOPS + MID_STOPS + FAR_STOPS, noise_db=3.0, seed=2)
    three = two + _measurements(NEAR_STOPS + MID_STOPS + FAR_STOPS, noise_db=3.0, seed=3)
    first, second, third = area(one), area(two), area(three)
    assert second < first
    assert third < second


def test_too_few_detections_refuses_rather_than_guessing() -> None:
    result = solve_site(_measurements(NEAR_STOPS[:2]), fast_solve_settings())
    assert result.status == STATUS_INSUFFICIENT_EVIDENCE
    assert result.surface is None
    assert "at least 3" in result.status_reason


def test_detections_from_one_direction_are_reported_as_weak_geometry() -> None:
    """Position and transmit power trade against each other along one bearing."""
    arc = [(32.0500, 34.7300), (32.0505, 34.7310), (32.0495, 34.7290), (32.0510, 34.7320)]
    distant_silence = [(31.80, 34.80), (32.30, 34.80), (32.05, 35.10)]
    settings = fast_solve_settings(margin_m=8_000.0)
    result = solve_site(
        _measurements(arc + distant_silence, reference_level_db=55.0, exponent=4.5), settings
    )
    assert result.status in (STATUS_WEAK_GEOMETRY, STATUS_UNBOUNDED_REGION)
    if result.status == STATUS_WEAK_GEOMETRY:
        assert result.azimuth_span_deg < settings.min_azimuth_span_deg


def test_detections_with_no_non_detections_do_not_close_a_region() -> None:
    result = solve_site(
        _measurements(NEAR_STOPS, threshold_db=-200.0), fast_solve_settings()
    )
    assert result.non_detection_count == 0
    assert result.status == STATUS_UNBOUNDED_REGION
    assert any("bounded from the inside only" in warning for warning in result.warnings)


def test_residuals_are_reported_for_every_measurement() -> None:
    measurements = _measurements(NEAR_STOPS + MID_STOPS + FAR_STOPS, noise_db=3.0, seed=5)
    result = solve_site(measurements, fast_solve_settings())
    assert len(result.residuals) == len(measurements)
    detected = [row for row in result.residuals if row["detected"]]
    assert all("residual_db" in row for row in detected)
    assert all("exceedance_db" in row for row in result.residuals if not row["detected"])


def test_invalid_measurements_are_rejected() -> None:
    with pytest.raises(ValueError, match="latitude out of range"):
        solve_site(
            [GeoMeasurement("bad", 120.0, 34.0, True, 10.0, 4.0)], fast_solve_settings()
        )
    with pytest.raises(ValueError, match="must carry a level"):
        solve_site(
            [GeoMeasurement("bad", 32.0, 34.0, True, None, 4.0)], fast_solve_settings()
        )


def test_a_pinned_path_loss_exponent_is_warned_about() -> None:
    result = solve_site(
        _measurements(NEAR_STOPS + MID_STOPS + FAR_STOPS, exponent=6.0),
        fast_solve_settings(path_loss_exponents=(2.0, 2.5)),
    )
    assert any("edge of the searched range" in warning for warning in result.warnings)


# --------------------------------------------------------------- contours


def _surface(probability: np.ndarray, grid: Grid) -> PosteriorSurface:
    return PosteriorSurface(
        grid=grid,
        projection=LocalProjection(32.0, 34.8),
        probability=(probability / probability.sum()).reshape(-1),
    )


def _demo_grid() -> tuple[Grid, np.ndarray, np.ndarray]:
    grid = Grid(x_min=-10_000.0, y_min=-10_000.0, resolution_m=200.0, nx=101, ny=101)
    xs, ys = grid.axes()
    return grid, *np.meshgrid(xs, ys)


def test_highest_density_threshold_selects_the_requested_mass() -> None:
    probability = np.array([0.4, 0.3, 0.2, 0.1])
    assert highest_density_threshold(probability, 0.7) == pytest.approx(0.3)
    assert highest_density_threshold(probability, 0.95) == pytest.approx(0.1)


def test_an_annulus_becomes_one_polygon_with_a_hole() -> None:
    grid, x, y = _demo_grid()
    radius = np.hypot(x, y)
    surface = _surface(np.exp(-((radius - 3000.0) ** 2) / (2 * 400.0**2)), grid)
    region = credible_regions(surface, (0.9,))[0]
    assert len(region["polygons"]) == 1
    assert len(region["polygons"][0]) == 2, "expected an exterior ring plus a hole"


def test_two_separated_modes_stay_two_polygons() -> None:
    grid, x, y = _demo_grid()
    probability = np.exp(-(((x - 4000) ** 2 + y**2) / (2 * 500.0**2))) + np.exp(
        -(((x + 4000) ** 2 + y**2) / (2 * 500.0**2))
    )
    region = credible_regions(_surface(probability, grid), (0.9,))[0]
    assert len(region["polygons"]) == 2
    assert all(len(polygon) == 1 for polygon in region["polygons"])


def test_ring_orientation_follows_geojson() -> None:
    grid, x, y = _demo_grid()
    radius = np.hypot(x, y)
    region = credible_regions(
        _surface(np.exp(-((radius - 3000.0) ** 2) / (2 * 400.0**2)), grid), (0.9,)
    )[0]
    exterior, hole = region["polygons"][0]

    def signed_area(ring: list[list[float]]) -> float:
        return 0.5 * sum(
            ring[i][0] * ring[(i + 1) % len(ring)][1] - ring[(i + 1) % len(ring)][0] * ring[i][1]
            for i in range(len(ring))
        )

    assert signed_area(exterior) > 0, "exterior rings are counter-clockwise"
    assert signed_area(hole) < 0, "interior rings are clockwise"


def test_area_comes_from_cell_counts_not_the_polygon() -> None:
    grid, x, y = _demo_grid()
    probability = np.exp(-((x**2 + y**2) / (2 * 1000.0**2)))
    region = credible_regions(_surface(probability, grid), (0.5,))[0]
    assert region["area_km2"] == pytest.approx(
        region["cell_count"] * (grid.resolution_m / 1000.0) ** 2
    )


def test_geojson_shape_is_valid() -> None:
    grid, x, y = _demo_grid()
    probability = np.exp(-((x**2 + y**2) / (2 * 1000.0**2)))
    payload = regions_to_geojson(
        credible_regions(_surface(probability, grid), (0.5, 0.9)), {"site_key": "S"}
    )
    assert payload["type"] == "FeatureCollection"
    assert len(payload["features"]) == 2
    for feature in payload["features"]:
        assert feature["geometry"]["type"] == "MultiPolygon"
        assert feature["properties"]["site_key"] == "S"
        first = feature["geometry"]["coordinates"][0][0]
        assert first[0] == first[-1], "rings are closed"
        assert -180 <= first[0][0] <= 180 and -90 <= first[0][1] <= 90
