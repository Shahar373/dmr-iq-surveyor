from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np

from dmr_iq_surveyor.detect.core import (
    DetectionSettings,
    classify_features,
    confidence_components,
    confidence_score,
    nearest_raster_hz,
    weighted_quantile,
)
from dmr_iq_surveyor.detect.io import load_spectrum


def feature_at(
    data: dict[str, Any],
    scan_center_hz: float,
    settings: DetectionSettings,
) -> dict[str, Any] | None:
    frequency = data["frequency_hz"]
    half_width = (
        settings.integration_width_hz / 2.0
        + settings.merge_tolerance_hz
    )
    mask = np.abs(frequency - scan_center_hz) <= half_width
    if np.count_nonzero(mask) < 5:
        return None
    f = frequency[mask]
    average_db = data["average_db"][mask]
    percentile_db = data["percentile_db"][mask]
    noise_db = data["noise_db"][mask]
    occupancy = data["occupancy_pct"][mask]
    edge = data["edge_mask"][mask]
    dc = data["dc_mask"][mask]
    resolution_hz = float(np.median(np.diff(f)))

    average_linear = np.power(10.0, average_db / 10.0)
    percentile_linear = np.power(10.0, percentile_db / 10.0)
    noise_linear = np.power(10.0, noise_db / 10.0)
    percentile_excess = np.maximum(
        percentile_linear - noise_linear,
        0.0,
    )

    average_snr_db = 10.0 * np.log10(
        (float(np.mean(average_linear)) + 1e-300)
        / (float(np.mean(noise_linear)) + 1e-300)
    )
    p95_snr_db = 10.0 * np.log10(
        (float(np.mean(percentile_linear)) + 1e-300)
        / (float(np.mean(noise_linear)) + 1e-300)
    )
    total_excess = float(np.sum(percentile_excess))
    measured_center = float(scan_center_hz)
    if total_excess > 0:
        measured_center = float(
            np.sum(f * percentile_excess) / total_excess
        )
    q05 = weighted_quantile(f, percentile_excess, 0.05)
    q95 = weighted_quantile(f, percentile_excess, 0.95)
    width_90 = max(resolution_hz, q95 - q05 + resolution_hz)
    peak_excess = float(np.max(percentile_excess))
    equivalent_width = (
        total_excess / max(peak_excess, 1e-300) * resolution_hz
    )
    peak_to_mean = 10.0 * np.log10(
        max(peak_excess, 1e-300)
        / (float(np.mean(percentile_excess)) + 1e-300)
    )
    bin_snr = 10.0 * np.log10(
        (percentile_linear + 1e-300) / (noise_linear + 1e-300)
    )
    fill_ratio = float(
        np.mean(bin_snr >= settings.fill_threshold_db)
    )
    left = float(np.sum(percentile_excess[f < measured_center]))
    right = float(np.sum(percentile_excess[f > measured_center]))
    symmetry = 1.0 - abs(left - right) / max(left + right, 1e-300)

    features: dict[str, Any] = {
        "scan_center_hz": float(scan_center_hz),
        "measured_center_hz": measured_center,
        "average_snr_db": float(average_snr_db),
        "p95_snr_db": float(p95_snr_db),
        "occupancy_pct": float(np.mean(occupancy)),
        "width_90_hz": float(width_90),
        "equivalent_width_hz": float(equivalent_width),
        "spectral_fill_ratio": fill_ratio,
        "symmetry_score": float(np.clip(symmetry, 0.0, 1.0)),
        "peak_to_channel_mean_db": float(peak_to_mean),
        "edge_warning": bool(np.any(edge)),
        "dc_warning": bool(np.any(dc)),
        "passband_warning": bool(
            measured_center < settings.passband_warning_low_hz
            or measured_center > settings.passband_warning_high_hz
        ),
    }
    features["preliminary_class"] = classify_features(
        features,
        settings,
    )
    components = confidence_components(features, settings)
    features["confidence_components"] = components
    features["confidence"] = confidence_score(components)
    features["frequency_hz_assuming_iq"] = nearest_raster_hz(
        measured_center,
        settings.scan_step_hz,
    )
    return features


def _is_strong(
    feature: dict[str, Any],
    settings: DetectionSettings,
) -> bool:
    return bool(
        feature["p95_snr_db"] >= settings.min_p95_channel_snr_db
        or feature["average_snr_db"]
        >= settings.min_average_channel_snr_db
    )


def _local_maxima(
    rows: list[dict[str, Any]],
    settings: DetectionSettings,
) -> list[dict[str, Any]]:
    retained: list[dict[str, Any]] = []
    for row in rows:
        neighbours = [
            other
            for other in rows
            if abs(
                float(other["scan_center_hz"])
                - float(row["scan_center_hz"])
            )
            <= settings.scan_step_hz + 1.0
        ]
        highest = max(
            (
                float(other["p95_snr_db"]),
                float(other["confidence"]),
            )
            for other in neighbours
        )
        current = (
            float(row["p95_snr_db"]),
            float(row["confidence"]),
        )
        if current == highest:
            retained.append(row)
    return retained


def _primary_candidate(
    feature: dict[str, Any],
    settings: DetectionSettings,
) -> bool:
    class_name = feature["preliminary_class"]
    if class_name in {
        "dmr_like_narrowband",
        "intermittent_narrowband",
    }:
        return True
    if class_name == "narrow_carrier_or_spur":
        return bool(
            feature["p95_snr_db"]
            >= settings.min_p95_channel_snr_db + 3.0
        )
    if class_name == "wideband_unknown":
        return bool(
            feature["p95_snr_db"]
            >= settings.min_p95_channel_snr_db
            and feature["equivalent_width_hz"] <= 6000.0
        )
    return False


def detect_spectrum(
    spectrum_dir: str | Path,
    settings: DetectionSettings | None = None,
) -> dict[str, Any]:
    source = Path(spectrum_dir).expanduser().resolve()
    resolved = settings or DetectionSettings()
    resolved.validate()
    data = load_spectrum(source)
    frequency = data["frequency_hz"]
    low = nearest_raster_hz(
        float(frequency[0]),
        resolved.scan_step_hz,
    )
    if low < frequency[0]:
        low += resolved.scan_step_hz
    high = (
        np.floor(float(frequency[-1]) / resolved.scan_step_hz)
        * resolved.scan_step_hz
    )

    strong_rows: list[dict[str, Any]] = []
    scan_centers = np.arange(
        low,
        high + resolved.scan_step_hz / 2.0,
        resolved.scan_step_hz,
    )
    for scan_center in scan_centers:
        feature = feature_at(data, float(scan_center), resolved)
        if feature is not None and _is_strong(feature, resolved):
            strong_rows.append(feature)

    maxima = _local_maxima(strong_rows, resolved)
    candidates = [
        feature
        for feature in maxima
        if _primary_candidate(feature, resolved)
    ]
    rejected = [
        feature
        for feature in maxima
        if not _primary_candidate(feature, resolved)
    ]
    candidates.sort(key=lambda row: row["frequency_hz_assuming_iq"])
    rejected.sort(key=lambda row: row["frequency_hz_assuming_iq"])
    report = data["report"]
    return {
        "spectrum_dir": str(source),
        "recording": report["recording"],
        "settings": resolved.to_dict(),
        "scanned_center_count": len(scan_centers),
        "strong_window_count": len(strong_rows),
        "local_maximum_count": len(maxima),
        "candidates": candidates,
        "rejected": rejected,
        "frequency_hz": frequency,
        "average_db": data["average_db"],
        "percentile_db": data["percentile_db"],
        "waterfall_db": data["waterfall_db"],
        "waterfall_frequency_hz": data["waterfall_frequency_hz"],
        "waterfall_time_s": data["waterfall_time_s"],
    }
