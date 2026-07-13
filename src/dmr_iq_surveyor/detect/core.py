from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

import numpy as np


@dataclass(slots=True)
class DetectionSettings:
    scan_step_hz: float = 6250.0
    integration_width_hz: float = 10000.0
    min_p95_channel_snr_db: float = 9.0
    min_average_channel_snr_db: float = 4.0
    min_equivalent_width_hz: float = 1500.0
    min_width_90_hz: float = 3000.0
    max_width_90_hz: float = 12000.0
    max_peak_to_channel_mean_db: float = 13.0
    min_fill_ratio: float = 0.20
    fill_threshold_db: float = 6.0
    merge_tolerance_hz: float = 3000.0
    passband_warning_low_hz: float = 159_490_000.0
    passband_warning_high_hz: float = 167_680_000.0

    def validate(self) -> None:
        if self.scan_step_hz <= 0:
            raise ValueError("scan_step_hz must be positive")
        if self.integration_width_hz <= 0:
            raise ValueError("integration_width_hz must be positive")
        if self.min_width_90_hz <= 0:
            raise ValueError("min_width_90_hz must be positive")
        if self.max_width_90_hz <= self.min_width_90_hz:
            raise ValueError("max_width_90_hz must exceed min_width_90_hz")
        if not 0 <= self.min_fill_ratio <= 1:
            raise ValueError("min_fill_ratio must be between 0 and 1")
        if self.merge_tolerance_hz <= 0:
            raise ValueError("merge_tolerance_hz must be positive")
        if self.passband_warning_high_hz <= self.passband_warning_low_hz:
            raise ValueError("passband warning limits are invalid")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def nearest_raster_hz(frequency_hz: float, spacing_hz: float) -> float:
    return float(np.floor(frequency_hz / spacing_hz + 0.5) * spacing_hz)


def mirrored_frequency_hz(
    frequency_hz: float,
    center_frequency_hz: float,
) -> float:
    return float(2.0 * center_frequency_hz - frequency_hz)


def weighted_quantile(
    values: np.ndarray,
    weights: np.ndarray,
    quantile: float,
) -> float:
    if values.ndim != 1 or weights.ndim != 1:
        raise ValueError("values and weights must be 1D arrays")
    if len(values) != len(weights):
        raise ValueError("values and weights must have equal length")
    if not 0 <= quantile <= 1:
        raise ValueError("quantile must be between 0 and 1")
    total = float(np.sum(weights))
    if total <= 0:
        return float(np.median(values))
    order = np.argsort(values)
    sorted_values = values[order]
    cumulative = np.cumsum(weights[order]) / total
    return float(np.interp(quantile, cumulative, sorted_values))


def classify_features(
    features: dict[str, float],
    settings: DetectionSettings,
) -> str:
    p95_ok = features["p95_snr_db"] >= settings.min_p95_channel_snr_db
    average_ok = (
        features["average_snr_db"] >= settings.min_average_channel_snr_db
    )
    if not p95_ok and not average_ok:
        return "noise_or_artifact"
    if (
        features["equivalent_width_hz"] < settings.min_equivalent_width_hz
        or features["width_90_hz"] < settings.min_width_90_hz
        or features["peak_to_channel_mean_db"]
        > settings.max_peak_to_channel_mean_db
    ):
        return "narrow_carrier_or_spur"
    if features["width_90_hz"] > settings.max_width_90_hz:
        return "wideband_unknown"
    if features["spectral_fill_ratio"] < settings.min_fill_ratio:
        return "noise_or_artifact"
    if average_ok:
        return "dmr_like_narrowband"
    return "intermittent_narrowband"


def confidence_components(
    features: dict[str, float],
    settings: DetectionSettings,
) -> dict[str, float]:
    p95_snr = np.clip(
        (
            features["p95_snr_db"]
            - settings.min_p95_channel_snr_db
        )
        / 18.0,
        0.0,
        1.0,
    )
    average_snr = np.clip(
        (
            features["average_snr_db"]
            - settings.min_average_channel_snr_db
        )
        / 16.0,
        0.0,
        1.0,
    )
    width_mid = (
        settings.min_width_90_hz + settings.max_width_90_hz
    ) / 2.0
    width_half = (
        settings.max_width_90_hz - settings.min_width_90_hz
    ) / 2.0
    width = np.clip(
        1.0
        - abs(features["width_90_hz"] - width_mid) / width_half,
        0.0,
        1.0,
    )
    equivalent_width = np.clip(
        features["equivalent_width_hz"]
        / (settings.min_equivalent_width_hz * 2.0),
        0.0,
        1.0,
    )
    fill = np.clip(
        (
            features["spectral_fill_ratio"]
            - settings.min_fill_ratio
        )
        / (1.0 - settings.min_fill_ratio),
        0.0,
        1.0,
    )
    symmetry = np.clip(features["symmetry_score"], 0.0, 1.0)
    occupancy = np.clip(features["occupancy_pct"] / 50.0, 0.0, 1.0)
    shape = np.clip(
        1.0
        - features["peak_to_channel_mean_db"]
        / settings.max_peak_to_channel_mean_db,
        0.0,
        1.0,
    )
    return {
        "p95_snr": float(p95_snr),
        "average_snr": float(average_snr),
        "width": float(width),
        "equivalent_width": float(equivalent_width),
        "fill": float(fill),
        "symmetry": float(symmetry),
        "occupancy": float(occupancy),
        "shape": float(shape),
    }


def confidence_score(components: dict[str, float]) -> float:
    weights = {
        "p95_snr": 0.20,
        "average_snr": 0.12,
        "width": 0.12,
        "equivalent_width": 0.14,
        "fill": 0.10,
        "symmetry": 0.10,
        "occupancy": 0.10,
        "shape": 0.12,
    }
    return float(
        sum(components[key] * weight for key, weight in weights.items())
    )
