"""The emitter solve stays bounded in memory as the measurement count grows.

`geo emitter solve` without `--campaign`, or a long Locate campaign, can hand
the solver thousands of measurements, and the Locate page runs that solve on a
Raspberry Pi. Two things used to scale with the count squared or worse:

* `shadowing_weights` built dense N x N x 2 and N x N arrays (~1 GB at 5,000);
* `_evaluate_grid` sized its cell chunk without counting measurements, so its
  (cells x measurements) temporaries reached ~2.6 GB each at 5,000.

Both are now blocked. These tests pin that the answers did not change and that
peak memory no longer follows N.
"""

from __future__ import annotations

import tracemalloc

import numpy as np
import pytest

from dmr_iq_surveyor.geo.model import GeoMeasurement, SolveSettings
from dmr_iq_surveyor.geo.solver import shadowing_weights, solve_site


def _dense_weights(east: np.ndarray, north: np.ndarray, correlation_m: float) -> np.ndarray:
    """The original all-pairs formula, kept here as the reference."""
    points = np.column_stack([east, north])
    deltas = points[:, None, :] - points[None, :, :]
    distances = np.hypot(deltas[..., 0], deltas[..., 1])
    return 1.0 / np.exp(-distances / correlation_m).sum(axis=1)


def _peak_bytes(function, *args, **kwargs) -> tuple[object, int]:
    tracemalloc.start()
    try:
        tracemalloc.reset_peak()
        result = function(*args, **kwargs)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    return result, peak


@pytest.mark.parametrize("block_elements", [1, 7, 64, 1 << 20])
def test_blocked_shadowing_weights_equal_the_dense_formula(block_elements: int) -> None:
    rng = np.random.default_rng(20261112)
    for case in range(40):
        count = int(rng.integers(1, 120))
        scale = 10.0 ** rng.uniform(1.0, 5.5)
        east = rng.uniform(-scale, scale, count)
        north = rng.uniform(-scale, scale, count)
        if case % 4 == 0:
            # Repeated stops: ties in the sort key, and weights of 1/k.
            east[: count // 2] = east[0]
            north[: count // 2] = north[0]
        correlation_m = 10.0 ** rng.uniform(1.0, 4.0)
        blocked = shadowing_weights(east, north, correlation_m, block_elements=block_elements)
        assert blocked is not None
        np.testing.assert_allclose(
            blocked, _dense_weights(east, north, correlation_m), rtol=1e-12, atol=0.0
        )


def test_points_beyond_the_cutoff_still_match_the_dense_formula() -> None:
    """Stops tens of correlation lengths apart are skipped, and that is exact
    to well within tolerance: each skipped pair is worth exp(-40)."""
    correlation_m = 100.0
    east = np.array([0.0, 50.0, 3_900.0, 4_100.0, 20_000.0, 1e6])
    north = np.array([0.0, 0.0, 0.0, 10.0, -5.0, 0.0])
    blocked = shadowing_weights(east, north, correlation_m, block_elements=2)
    np.testing.assert_allclose(
        blocked, _dense_weights(east, north, correlation_m), rtol=1e-12, atol=0.0
    )
    assert blocked[-1] == pytest.approx(1.0)


def test_shadowing_weights_edge_cases() -> None:
    assert shadowing_weights(np.zeros(3), np.zeros(3), 0.0) is None
    empty = shadowing_weights(np.empty(0), np.empty(0), 500.0)
    assert empty is not None and empty.size == 0
    assert shadowing_weights(np.zeros(1), np.zeros(1), 500.0) == pytest.approx([1.0])
    with pytest.raises(ValueError, match="block_elements"):
        shadowing_weights(np.zeros(2), np.zeros(2), 500.0, block_elements=0)


def test_shadowing_weights_at_large_n_stay_within_a_memory_bound() -> None:
    """6,000 stops in a 4 km square: every pair lies inside the cutoff, the
    worst case for blocking. The dense form needed ~1.15 GB here."""
    rng = np.random.default_rng(7)
    count = 6_000
    east = rng.uniform(-2_000.0, 2_000.0, count)
    north = rng.uniform(-2_000.0, 2_000.0, count)
    weights, peak = _peak_bytes(shadowing_weights, east, north, 500.0)
    assert peak < 64 * 1024 * 1024, f"peak {peak / 1e6:.0f} MB"
    assert weights is not None and weights.shape == (count,)
    assert np.all((weights > 0.0) & (weights <= 1.0))
    # Spot-check a few rows against the exact sum.
    for index in (0, count // 2, count - 1):
        distances = np.hypot(east - east[index], north - north[index])
        assert weights[index] == pytest.approx(
            1.0 / np.exp(-distances / 500.0).sum(), rel=1e-12
        )


def _drive(count: int) -> list[GeoMeasurement]:
    """`count` detections along a 3 km arc around a transmitter."""
    rng = np.random.default_rng(3)
    angle = np.linspace(0.0, 1.5 * np.pi, count)
    radius = 3_000.0 + rng.normal(0.0, 50.0, count)
    east, north = radius * np.cos(angle), radius * np.sin(angle)
    level = 40.0 - 35.0 * np.log10(np.hypot(east - 500.0, north - 200.0) / 1_000.0)
    level = level + rng.normal(0.0, 4.0, count)
    latitude = 32.0 + north / 111_000.0
    longitude = 34.8 + east / (111_000.0 * np.cos(np.radians(32.0)))
    return [
        GeoMeasurement(
            label=f"bin{index}",
            latitude=float(latitude[index]),
            longitude=float(longitude[index]),
            detected=True,
            level_db=float(level[index]),
            censor_level_db=0.0,
            survey_run_id=f"bin{index}",
        )
        for index in range(count)
    ]


def test_solve_peak_memory_does_not_follow_the_measurement_count() -> None:
    """Four times the measurements, about the same peak.

    Before the chunk accounted for measurements, the peak grew in proportion
    (877 MB at 2,000 drive bins on this grid, against 160 MB now).
    """
    settings = SolveSettings(
        margin_m=4_000.0,
        coarse_resolution_m=1_000.0,
        resolution_m=250.0,
        target_fine_cells=1_500,
        max_fine_cells=5_000,
        max_coarse_cells=5_000,
        shadowing_correlation_m=500.0,
        max_working_elements=500_000,
    )
    small, small_peak = _peak_bytes(solve_site, _drive(300), settings)
    large, large_peak = _peak_bytes(solve_site, _drive(1_200), settings)
    assert small.surface is not None and large.surface is not None
    assert large_peak < 1.5 * small_peak, (
        f"{small_peak / 1e6:.0f} MB at 300, {large_peak / 1e6:.0f} MB at 1,200"
    )
    assert large_peak < 96 * 1024 * 1024, f"peak {large_peak / 1e6:.0f} MB"
