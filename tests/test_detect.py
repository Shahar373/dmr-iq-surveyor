from __future__ import annotations

import numpy as np

from dmr_iq_surveyor.detect.core import (
    DetectionSettings,
    classify_features,
    mirrored_frequency_hz,
    nearest_raster_hz,
    weighted_quantile,
)
from dmr_iq_surveyor.detect.merge import candidate_clusters


def test_raster_and_mirror() -> None:
    assert nearest_raster_hz(164_299_900.0, 6250.0) == 164_300_000.0
    assert nearest_raster_hz(164_306_100.0, 12500.0) == 164_300_000.0
    assert mirrored_frequency_hz(
        164_300_000.0,
        163_671_500.0,
    ) == 163_043_000.0


def test_weighted_quantile() -> None:
    values = np.array([0.0, 1.0, 2.0, 3.0])
    weights = np.array([0.0, 1.0, 8.0, 1.0])
    result = weighted_quantile(values, weights, 0.5)
    assert 1.0 < result <= 2.0


def test_classifies_dmr_shape() -> None:
    settings = DetectionSettings()
    features = {
        "p95_snr_db": 18.0,
        "average_snr_db": 12.0,
        "equivalent_width_hz": 2600.0,
        "width_90_hz": 5000.0,
        "peak_to_channel_mean_db": 7.0,
        "spectral_fill_ratio": 0.75,
    }
    assert classify_features(features, settings) == "dmr_like_narrowband"


def test_rejects_narrow_spur() -> None:
    settings = DetectionSettings()
    features = {
        "p95_snr_db": 30.0,
        "average_snr_db": 20.0,
        "equivalent_width_hz": 700.0,
        "width_90_hz": 1200.0,
        "peak_to_channel_mean_db": 16.0,
        "spectral_fill_ratio": 0.9,
    }
    assert classify_features(features, settings) == "narrow_carrier_or_spur"


def test_clusters_duplicates_but_not_adjacent_channels() -> None:
    rows = [
        {
            "frequency_hz_assuming_iq": 164_300_000.0,
            "measured_center_hz": 164_299_500.0,
        },
        {
            "frequency_hz_assuming_iq": 164_300_000.0,
            "measured_center_hz": 164_300_600.0,
        },
        {
            "frequency_hz_assuming_iq": 164_312_500.0,
            "measured_center_hz": 164_312_500.0,
        },
    ]
    clusters = candidate_clusters(rows, 3000.0)
    assert [len(cluster) for cluster in clusters] == [2, 1]
