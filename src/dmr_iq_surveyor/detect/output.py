from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

from dmr_iq_surveyor.detect.core import DetectionSettings


def _write_csv(
    path: Path,
    candidates: list[dict[str, Any]],
) -> None:
    fields = [
        "candidate_id",
        "frequency_hz_assuming_iq",
        "frequency_hz_if_qi",
        "measured_center_hz",
        "nearest_6k25_hz",
        "nearest_12k5_hz",
        "offset_from_6k25_hz",
        "offset_from_12k5_hz",
        "average_snr_db",
        "p95_snr_db",
        "occupancy_pct",
        "width_90_hz",
        "equivalent_width_hz",
        "spectral_fill_ratio",
        "symmetry_score",
        "peak_to_channel_mean_db",
        "recordings_seen",
        "first_recording_seen",
        "last_recording_seen",
        "passband_warning",
        "dc_warning",
        "edge_warning",
        "preliminary_class",
        "confidence",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for candidate in candidates:
            writer.writerow(
                {key: candidate[key] for key in fields}
            )


def _json_ready(value: Any) -> Any:
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, dict):
        return {
            key: _json_ready(item) for key, item in value.items()
        }
    if isinstance(value, list):
        return [_json_ready(item) for item in value]
    return value


def _plot_average(
    path: Path,
    frequency: np.ndarray,
    values: np.ndarray,
    candidates: list[dict[str, Any]],
) -> None:
    plt.figure(figsize=(16, 7))
    plt.plot(frequency / 1e6, values, linewidth=0.65)
    ymin, ymax = plt.ylim()
    label_y = ymax - 0.04 * (ymax - ymin)
    for candidate in candidates:
        frequency_mhz = (
            float(candidate["frequency_hz_assuming_iq"]) / 1e6
        )
        plt.axvline(frequency_mhz, alpha=0.25, linewidth=0.8)
        plt.text(
            frequency_mhz,
            label_y,
            candidate["candidate_id"],
            rotation=90,
            ha="center",
            va="top",
            fontsize=6,
        )
    plt.xlabel("Frequency (MHz)")
    plt.ylabel("PSD (dBFS/Hz, relative)")
    plt.title("Candidate detections — combined average spectrum")
    plt.grid(alpha=0.2)
    plt.tight_layout()
    plt.savefig(path, dpi=150)
    plt.close()


def _plot_waterfall(
    path: Path,
    result: dict[str, Any],
    candidates: list[dict[str, Any]],
) -> None:
    frequency = result["waterfall_frequency_hz"]
    time_s = result["waterfall_time_s"]
    extent = [
        float(frequency[0]) / 1e6,
        float(frequency[-1]) / 1e6,
        float(time_s[0]),
        float(time_s[-1]),
    ]
    plt.figure(figsize=(16, 8))
    image = plt.imshow(
        result["waterfall_db"],
        origin="lower",
        aspect="auto",
        extent=extent,
        interpolation="nearest",
    )
    for candidate in candidates:
        frequency_mhz = (
            float(candidate["frequency_hz_assuming_iq"]) / 1e6
        )
        plt.axvline(frequency_mhz, alpha=0.5, linewidth=0.8)
    plt.xlabel("Frequency (MHz)")
    plt.ylabel("Time from recording start (s)")
    plt.title("Candidate detections — first recording waterfall")
    plt.colorbar(image, label="PSD (dBFS/Hz, relative)")
    plt.tight_layout()
    plt.savefig(path, dpi=150)
    plt.close()


def write_detection_outputs(
    output_dir: str | Path,
    candidates: list[dict[str, Any]],
    results: list[tuple[str, dict[str, Any]]],
    settings: DetectionSettings,
) -> dict[str, Any]:
    destination = Path(output_dir).expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)
    _write_csv(destination / "candidates.csv", candidates)
    public_candidates = [
        {
            key: value
            for key, value in candidate.items()
            if key != "evidence"
        }
        for candidate in candidates
    ]
    (destination / "candidates.json").write_text(
        json.dumps(_json_ready(public_candidates), indent=2),
        encoding="utf-8",
    )
    evidence = {
        candidate["candidate_id"]: candidate["evidence"]
        for candidate in candidates
    }
    (destination / "candidate_evidence.json").write_text(
        json.dumps(_json_ready(evidence), indent=2),
        encoding="utf-8",
    )
    rejected = {
        recording_id: result["rejected"]
        for recording_id, result in results
    }
    (destination / "rejected_evidence.json").write_text(
        json.dumps(_json_ready(rejected), indent=2),
        encoding="utf-8",
    )

    if results:
        common_frequency = results[0][1]["frequency_hz"]
        average_stack = np.vstack(
            [result["average_db"] for _, result in results]
        )
        _plot_average(
            destination / "average_spectrum_annotated.png",
            common_frequency,
            np.max(average_stack, axis=0),
            candidates,
        )
        _plot_waterfall(
            destination / "waterfall_annotated.png",
            results[0][1],
            candidates,
        )

    classes: dict[str, int] = {}
    for candidate in candidates:
        class_name = candidate["preliminary_class"]
        classes[class_name] = classes.get(class_name, 0) + 1
    rows = [
        (
            "| ID | Frequency MHz | Seen | Class | P95 SNR dB | "
            "Width Hz | Confidence | Warnings |"
        ),
        "|---|---:|---:|---|---:|---:|---:|---|",
    ]
    for candidate in candidates:
        warning_names = []
        if candidate["passband_warning"]:
            warning_names.append("passband")
        if candidate["dc_warning"]:
            warning_names.append("dc")
        if candidate["edge_warning"]:
            warning_names.append("edge")
        warnings = ", ".join(warning_names) or "-"
        rows.append(
            "| {candidate_id} | {frequency:.6f} | {seen} | "
            "{class_name} | {p95:.2f} | {width:.0f} | "
            "{confidence:.3f} | {warnings} |".format(
                candidate_id=candidate["candidate_id"],
                frequency=(
                    candidate["frequency_hz_assuming_iq"] / 1e6
                ),
                seen=candidate["recordings_seen"],
                class_name=candidate["preliminary_class"],
                p95=candidate["p95_snr_db"],
                width=candidate["width_90_hz"],
                confidence=candidate["confidence"],
                warnings=warnings,
            )
        )
    report = f"""# Candidate detection report

- Candidates retained: **{len(candidates)}**
- Recordings analyzed independently: **{len(results)}**
- Preliminary classes: `{classes}`
- IQ order remains assumed; `frequency_hz_if_qi` preserves the mirrored alternative.
- No candidate is decoder-confirmed DMR in this phase.

{chr(10).join(rows)}
"""
    (destination / "candidate_report.md").write_text(
        report,
        encoding="utf-8",
    )
    return {
        "output_dir": str(destination),
        "candidate_count": len(candidates),
        "classes": classes,
        "settings": settings.to_dict(),
    }
