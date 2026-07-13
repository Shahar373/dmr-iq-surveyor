from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from dmr_iq_surveyor.inspection import run_inspection, write_json


@dataclass(slots=True)
class BatchRecording:
    recording_id: str
    path: Path
    label: str | None = None


class BatchConfigError(ValueError):
    """Raised when the batch configuration is invalid."""


def _safe_id(value: str) -> str:
    cleaned = "".join(char if char.isalnum() or char in "-_" else "-" for char in value.strip())
    cleaned = cleaned.strip("-")
    if not cleaned:
        raise BatchConfigError("recording id must contain at least one letter or digit")
    return cleaned


def load_batch_config(path: str | Path) -> dict[str, Any]:
    config_path = Path(path).expanduser().resolve()
    if not config_path.is_file():
        raise FileNotFoundError(config_path)
    payload = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise BatchConfigError("Batch configuration must be a YAML mapping")
    recordings_raw = payload.get("recordings")
    if not isinstance(recordings_raw, list) or not recordings_raw:
        raise BatchConfigError("Batch configuration must include a non-empty recordings list")

    recordings: list[BatchRecording] = []
    seen_ids: set[str] = set()
    for index, item in enumerate(recordings_raw, start=1):
        if not isinstance(item, dict):
            raise BatchConfigError(f"recordings[{index}] must be a mapping")
        raw_path = item.get("path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            raise BatchConfigError(f"recordings[{index}].path is required")
        source = Path(raw_path).expanduser().resolve()
        raw_id = item.get("id") or source.stem
        recording_id = _safe_id(str(raw_id))
        if recording_id in seen_ids:
            raise BatchConfigError(f"Duplicate recording id: {recording_id}")
        seen_ids.add(recording_id)
        recordings.append(
            BatchRecording(
                recording_id=recording_id,
                path=source,
                label=str(item["label"]) if item.get("label") is not None else None,
            )
        )

    project = payload.get("project") if isinstance(payload.get("project"), dict) else {}
    inspection = payload.get("inspection") if isinstance(payload.get("inspection"), dict) else {}
    output_root = Path(project.get("output_root", "runs/batch")).expanduser().resolve()
    return {
        "config_path": config_path,
        "project_name": str(project.get("name", config_path.stem)),
        "output_root": output_root,
        "recordings": recordings,
        "inspection": {
            "assumed_iq_order": str(inspection.get("assumed_iq_order", "IQ")),
            "statistics_window_frames": int(inspection.get("statistics_window_frames", 250_000)),
            "diagnostic_plot_frames": int(inspection.get("diagnostic_plot_frames", 20_000)),
            "compute_sha256": bool(inspection.get("compute_sha256", False)),
        },
    }


def _summary_row(
    recording: BatchRecording,
    status: str,
    result: dict[str, Any] | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "recording_id": recording.recording_id,
        "label": recording.label or "",
        "path": str(recording.path),
        "status": status,
        "error": error or "",
        "file_size_bytes": "",
        "container": "",
        "sample_rate_hz": "",
        "center_frequency_hz": "",
        "center_frequency_source": "",
        "frequency_low_hz": "",
        "frequency_high_hz": "",
        "duration_seconds": "",
        "frame_count": "",
        "bits_per_sample": "",
        "sample_encoding": "",
        "iq_order": "",
        "warning_count": "",
        "elapsed_seconds": "",
        "peak_rss_bytes": "",
        "output_dir": "",
    }
    if result is None:
        return row
    info = result["recording"]
    manifest = result["manifest"]
    row.update(
        {
            "file_size_bytes": info["file_size_bytes"],
            "container": info["container"],
            "sample_rate_hz": info["fmt"]["sample_rate_hz"],
            "center_frequency_hz": info["center_frequency_hz"],
            "center_frequency_source": info.get("center_frequency_source", "missing"),
            "frequency_low_hz": info["nominal_frequency_low_hz"],
            "frequency_high_hz": info["nominal_frequency_high_hz"],
            "duration_seconds": info["duration_seconds"],
            "frame_count": info["frame_count"],
            "bits_per_sample": info["fmt"]["bits_per_sample"],
            "sample_encoding": info["sample_encoding"],
            "iq_order": info["iq_order"],
            "warning_count": len(info["warnings"]) + len(result["statistics"].get("warnings", [])),
            "elapsed_seconds": manifest["elapsed_seconds"],
            "peak_rss_bytes": manifest["peak_rss_bytes"],
            "output_dir": manifest["output_dir"],
        }
    )
    return row


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fieldnames = list(rows[0].keys()) if rows else []
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _consistency(rows: list[dict[str, Any]]) -> dict[str, Any]:
    successful = [row for row in rows if row["status"] == "ok"]

    def unique(field: str) -> list[Any]:
        return sorted({row[field] for row in successful if row[field] not in {"", None}})

    centers = unique("center_frequency_hz")
    sample_rates = unique("sample_rate_hz")
    bits = unique("bits_per_sample")
    encodings = unique("sample_encoding")
    containers = unique("container")
    return {
        "successful_recordings": len(successful),
        "failed_recordings": len(rows) - len(successful),
        "same_center_frequency": len(centers) <= 1,
        "center_frequencies_hz": centers,
        "same_sample_rate": len(sample_rates) <= 1,
        "sample_rates_hz": sample_rates,
        "same_bits_per_sample": len(bits) <= 1,
        "bits_per_sample_values": bits,
        "same_encoding": len(encodings) <= 1,
        "encodings": encodings,
        "containers": containers,
    }


def _write_report(
    output_root: Path,
    project_name: str,
    rows: list[dict[str, Any]],
    consistency: dict[str, Any],
) -> None:
    table_lines = [
        "| ID | Status | Center MHz | Sample rate | Duration s | Warnings |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for row in rows:
        center_value = row.get("center_frequency_hz")
        center = f"{float(center_value) / 1e6:.6f}" if center_value not in {"", None} else "-"
        rate_value = row.get("sample_rate_hz")
        rate = f"{int(rate_value):,}" if rate_value not in {"", None} else "-"
        duration_value = row.get("duration_seconds")
        duration = f"{float(duration_value):.6f}" if duration_value not in {"", None} else "-"
        warnings = str(row["warning_count"]) if row["warning_count"] != "" else "-"
        table_lines.append(
            f"| `{row['recording_id']}` | {row['status']} | {center} | {rate} | {duration} | {warnings} |"
        )

    report = f"""# Batch IQ Inspection — {project_name}

## Result

- Successful recordings: **{consistency['successful_recordings']}**
- Failed recordings: **{consistency['failed_recordings']}**
- Same center frequency: **{consistency['same_center_frequency']}**
- Same sample rate: **{consistency['same_sample_rate']}**
- Same sample encoding: **{consistency['same_encoding']}**

## Recordings

{chr(10).join(table_lines)}

## Consistency values

- Center frequencies: `{consistency['center_frequencies_hz']}`
- Sample rates: `{consistency['sample_rates_hz']}`
- Bits per sample: `{consistency['bits_per_sample_values']}`
- Encodings: `{consistency['encodings']}`
- Containers: `{consistency['containers']}`

## Next review

Open each recording's `report.md` and both diagnostic PNG files. Do not start spectral detection until center frequency, sample rate, sample encoding, data integrity, and IQ assumptions have been reviewed.
"""
    (output_root / "batch_report.md").write_text(report, encoding="utf-8")


def run_batch_inspection(config_path: str | Path) -> dict[str, Any]:
    config = load_batch_config(config_path)
    output_root: Path = config["output_root"]
    output_root.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []

    for recording in config["recordings"]:
        destination = output_root / "recordings" / recording.recording_id
        try:
            result = run_inspection(recording.path, destination, **config["inspection"])
        except Exception as exc:
            rows.append(_summary_row(recording, "failed", error=f"{type(exc).__name__}: {exc}"))
            results.append(
                {
                    "recording_id": recording.recording_id,
                    "path": str(recording.path),
                    "status": "failed",
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            continue
        rows.append(_summary_row(recording, "ok", result=result))
        results.append(
            {
                "recording_id": recording.recording_id,
                "path": str(recording.path),
                "status": "ok",
                "output_dir": result["manifest"]["output_dir"],
                "recording": result["recording"],
            }
        )

    consistency = _consistency(rows)
    _write_csv(output_root / "batch_summary.csv", rows)
    write_json(
        output_root / "batch_summary.json",
        {
            "project_name": config["project_name"],
            "config_path": str(config["config_path"]),
            "output_root": str(output_root),
            "inspection": config["inspection"],
            "consistency": consistency,
            "recordings": results,
        },
    )
    _write_report(output_root, config["project_name"], rows, consistency)
    return {"output_root": str(output_root), "rows": rows, "consistency": consistency}
