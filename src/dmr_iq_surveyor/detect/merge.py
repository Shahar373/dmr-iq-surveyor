from __future__ import annotations

from typing import Any

import numpy as np

from dmr_iq_surveyor.detect.core import (
    DetectionSettings,
    mirrored_frequency_hz,
    nearest_raster_hz,
)


def _same_candidate(
    left: dict[str, Any],
    right: dict[str, Any],
    tolerance_hz: float,
) -> bool:
    if (
        left["frequency_hz_assuming_iq"]
        == right["frequency_hz_assuming_iq"]
    ):
        return True
    return bool(
        abs(
            float(left["measured_center_hz"])
            - float(right["measured_center_hz"])
        )
        <= tolerance_hz
    )


def candidate_clusters(
    rows: list[dict[str, Any]],
    tolerance_hz: float,
) -> list[list[dict[str, Any]]]:
    clusters: list[list[dict[str, Any]]] = []
    ordered = sorted(
        rows,
        key=lambda row: float(row["frequency_hz_assuming_iq"]),
    )
    for row in ordered:
        matching = next(
            (
                cluster
                for cluster in clusters
                if _same_candidate(cluster[0], row, tolerance_hz)
            ),
            None,
        )
        if matching is None:
            clusters.append([row])
        else:
            matching.append(row)
    return clusters


def _merged_class(classes: list[str]) -> str:
    priority = [
        "dmr_like_narrowband",
        "intermittent_narrowband",
        "narrow_carrier_or_spur",
        "wideband_unknown",
        "noise_or_artifact",
    ]
    return next(name for name in priority if name in classes)


def merge_recordings(
    results: list[tuple[str, dict[str, Any]]],
    settings: DetectionSettings,
) -> list[dict[str, Any]]:
    evidence_rows: list[dict[str, Any]] = []
    for recording_id, result in results:
        for candidate in result["candidates"]:
            row = dict(candidate)
            row["recording_id"] = recording_id
            row["recording_center_frequency_hz"] = result[
                "recording"
            ]["center_frequency_hz"]
            evidence_rows.append(row)

    merged: list[dict[str, Any]] = []
    clusters = candidate_clusters(
        evidence_rows,
        settings.merge_tolerance_hz,
    )
    for cluster in clusters:
        weights = np.asarray(
            [
                max(float(row["confidence"]), 0.01)
                for row in cluster
            ]
        )
        measured = float(
            np.average(
                [row["measured_center_hz"] for row in cluster],
                weights=weights,
            )
        )
        center_frequency = float(
            cluster[0]["recording_center_frequency_hz"]
        )
        assumed = nearest_raster_hz(
            measured,
            settings.scan_step_hz,
        )
        recordings = sorted(
            {str(row["recording_id"]) for row in cluster}
        )
        classes = [
            str(row["preliminary_class"]) for row in cluster
        ]
        components: dict[str, float] = {}
        for key in cluster[0]["confidence_components"]:
            components[key] = float(
                max(
                    row["confidence_components"][key]
                    for row in cluster
                )
            )
        persistence = len(recordings) / max(len(results), 1)
        components["persistence"] = persistence
        base_confidence = float(
            max(row["confidence"] for row in cluster)
        )
        confidence = float(
            np.clip(
                0.85 * base_confidence + 0.15 * persistence,
                0.0,
                1.0,
            )
        )

        def values(key: str) -> list[float]:
            return [float(row[key]) for row in cluster]

        nearest_6k25 = nearest_raster_hz(measured, 6250.0)
        nearest_12k5 = nearest_raster_hz(measured, 12500.0)
        merged.append(
            {
                "frequency_hz_assuming_iq": assumed,
                "frequency_hz_if_qi": mirrored_frequency_hz(
                    assumed,
                    center_frequency,
                ),
                "measured_center_hz": measured,
                "nearest_6k25_hz": nearest_6k25,
                "nearest_12k5_hz": nearest_12k5,
                "offset_from_6k25_hz": measured - nearest_6k25,
                "offset_from_12k5_hz": measured - nearest_12k5,
                "average_snr_db": max(values("average_snr_db")),
                "p95_snr_db": max(values("p95_snr_db")),
                "occupancy_pct": max(values("occupancy_pct")),
                "width_90_hz": float(
                    np.median(values("width_90_hz"))
                ),
                "equivalent_width_hz": float(
                    np.median(values("equivalent_width_hz"))
                ),
                "spectral_fill_ratio": max(
                    values("spectral_fill_ratio")
                ),
                "symmetry_score": max(values("symmetry_score")),
                "peak_to_channel_mean_db": min(
                    values("peak_to_channel_mean_db")
                ),
                "recordings_seen": len(recordings),
                "recording_ids": recordings,
                "first_recording_seen": recordings[0],
                "last_recording_seen": recordings[-1],
                "passband_warning": any(
                    bool(row["passband_warning"])
                    for row in cluster
                ),
                "dc_warning": any(
                    bool(row["dc_warning"]) for row in cluster
                ),
                "edge_warning": any(
                    bool(row["edge_warning"]) for row in cluster
                ),
                "preliminary_class": _merged_class(classes),
                "confidence": confidence,
                "confidence_components": components,
                "evidence": cluster,
            }
        )
    merged.sort(key=lambda row: row["frequency_hz_assuming_iq"])
    for index, row in enumerate(merged, start=1):
        row["candidate_id"] = f"C{index:04d}"
    return merged
