from __future__ import annotations

import csv
import platform
import resource
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import yaml

from dmr_iq_surveyor import __version__
from dmr_iq_surveyor.batch import BatchConfigError, BatchRecording, load_batch_config
from dmr_iq_surveyor.inspection import write_json
from dmr_iq_surveyor.iq.metadata import inspect_wave_iq
from dmr_iq_surveyor.iq.reader import IQMemmapReader
from dmr_iq_surveyor.spectrum.core import (
    SpectrumSettings,
    build_window,
    fft_frame_count,
    fft_step,
    frequency_axis_hz,
    iter_fft_starts,
    local_noise_floor_db,
    percentile_frame_indices,
    periodogram_power_density,
    power_to_db,
    reduce_frequency_bins,
)


@dataclass(slots=True)
class SpectrumArrays:
    frequency_hz: np.ndarray
    average_db: np.ndarray
    max_hold_db: np.ndarray
    percentile_db: np.ndarray
    noise_floor_db: np.ndarray
    occupancy_pct: np.ndarray
    waterfall_db: np.ndarray
    waterfall_frequency_hz: np.ndarray
    waterfall_time_s: np.ndarray
    edge_mask: np.ndarray
    dc_mask: np.ndarray


def _peak_rss_bytes() -> int:
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(value * 1024 if sys.platform != "darwin" else value)


def _settings_from_mapping(payload: dict[str, Any] | None) -> SpectrumSettings:
    payload = payload or {}
    allowed = {field for field in SpectrumSettings.__dataclass_fields__}
    unknown = set(payload) - allowed
    if unknown:
        raise BatchConfigError(f"Unknown spectrum settings: {sorted(unknown)}")
    settings = SpectrumSettings(**{key: payload[key] for key in payload if key in allowed})
    settings.validate()
    return settings


def _write_series_csv(
    path: Path,
    frequency_hz: np.ndarray,
    field_name: str,
    values: np.ndarray,
    edge_mask: np.ndarray,
    dc_mask: np.ndarray,
) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["frequency_hz", field_name, "edge_excluded", "dc_excluded"])
        for frequency, value, edge, dc in zip(
            frequency_hz, values, edge_mask, dc_mask, strict=True
        ):
            writer.writerow(
                [f"{float(frequency):.6f}", f"{float(value):.9f}", int(edge), int(dc)]
            )


def _write_occupancy_csv(
    path: Path,
    frequency_hz: np.ndarray,
    occupancy_pct: np.ndarray,
    edge_mask: np.ndarray,
    dc_mask: np.ndarray,
) -> None:
    _write_series_csv(
        path,
        frequency_hz,
        "occupancy_pct",
        occupancy_pct,
        edge_mask,
        dc_mask,
    )


def _plot_line(
    path: Path,
    frequency_hz: np.ndarray,
    values: np.ndarray,
    title: str,
    ylabel: str,
    center_hz: float,
    low_hz: float,
    high_hz: float,
    settings: SpectrumSettings,
) -> None:
    plt.figure(figsize=(14, 6))
    plt.plot(frequency_hz / 1e6, values, linewidth=0.7)
    if settings.edge_exclusion_hz > 0:
        plt.axvspan(low_hz / 1e6, (low_hz + settings.edge_exclusion_hz) / 1e6, alpha=0.12)
        plt.axvspan((high_hz - settings.edge_exclusion_hz) / 1e6, high_hz / 1e6, alpha=0.12)
    if settings.dc_exclusion_hz > 0:
        plt.axvspan(
            (center_hz - settings.dc_exclusion_hz) / 1e6,
            (center_hz + settings.dc_exclusion_hz) / 1e6,
            alpha=0.12,
        )
    plt.xlabel("Frequency (MHz)")
    plt.ylabel(ylabel)
    plt.title(title)
    plt.grid(alpha=0.2)
    plt.tight_layout()
    plt.savefig(path, dpi=150)
    plt.close()


def _plot_waterfall(path: Path, arrays: SpectrumArrays, title: str) -> None:
    plt.figure(figsize=(14, 7))
    extent = [
        arrays.waterfall_frequency_hz[0] / 1e6,
        arrays.waterfall_frequency_hz[-1] / 1e6,
        arrays.waterfall_time_s[0],
        arrays.waterfall_time_s[-1],
    ]
    image = plt.imshow(
        arrays.waterfall_db,
        origin="lower",
        aspect="auto",
        extent=extent,
        interpolation="nearest",
    )
    plt.xlabel("Frequency (MHz)")
    plt.ylabel("Time from recording start (s)")
    plt.title(title)
    plt.colorbar(image, label="PSD (dBFS/Hz, relative)")
    plt.tight_layout()
    plt.savefig(path, dpi=150)
    plt.close()


def _analyze(
    reader: IQMemmapReader, settings: SpectrumSettings
) -> tuple[SpectrumArrays, dict[str, Any]]:
    info = reader.info
    if info.center_frequency_hz is None:
        raise ValueError("Center frequency is required for spectral analysis")
    sample_rate = float(info.fmt.sample_rate_hz)
    count = fft_frame_count(info.frame_count, settings.fft_size, settings.overlap_ratio)
    if count < 1:
        raise ValueError(
            f"Recording has {info.frame_count} frames, fewer than fft_size={settings.fft_size}"
        )

    frequency_hz = frequency_axis_hz(
        float(info.center_frequency_hz), sample_rate, settings.fft_size
    )
    resolution_hz = sample_rate / settings.fft_size
    window = build_window(settings.window, settings.fft_size)
    average_sum = np.zeros(settings.fft_size, dtype=np.float64)
    max_hold = np.zeros(settings.fft_size, dtype=np.float64)
    noise_sum_db = np.zeros(settings.fft_size, dtype=np.float64)
    occupied = np.zeros(settings.fft_size, dtype=np.uint32)

    percentile_indices = percentile_frame_indices(count, settings.percentile_max_frames)
    percentile_lookup = {int(frame): row for row, frame in enumerate(percentile_indices)}
    percentile_samples_db = np.empty(
        (len(percentile_indices), settings.fft_size), dtype=np.float32
    )

    waterfall_time_bins = min(settings.waterfall_time_bins, count)
    waterfall_frequency_bins = min(settings.waterfall_frequency_bins, settings.fft_size)
    waterfall_sum = np.zeros(
        (waterfall_time_bins, waterfall_frequency_bins), dtype=np.float64
    )
    waterfall_counts = np.zeros(waterfall_time_bins, dtype=np.uint32)
    waterfall_frequency_hz: np.ndarray | None = None

    bins_per_noise_window = max(
        1, int(round(settings.local_noise_window_hz / resolution_hz))
    )
    starts = iter_fft_starts(info.frame_count, settings.fft_size, settings.overlap_ratio)

    for frame_index, start in enumerate(starts):
        samples = reader.read_complex(start, settings.fft_size)
        power = periodogram_power_density(samples, window, sample_rate)
        spectrum_db = power_to_db(power)
        floor_db = local_noise_floor_db(spectrum_db, bins_per_noise_window)

        average_sum += power
        np.maximum(max_hold, power, out=max_hold)
        noise_sum_db += floor_db
        occupied += spectrum_db > (floor_db + settings.occupancy_threshold_db)

        percentile_row = percentile_lookup.get(frame_index)
        if percentile_row is not None:
            percentile_samples_db[percentile_row] = spectrum_db.astype(np.float32)

        reduced_power, reduced_starts, reduced_widths = reduce_frequency_bins(
            power, waterfall_frequency_bins
        )
        if waterfall_frequency_hz is None:
            frequency_sums = np.add.reduceat(frequency_hz, reduced_starts)
            waterfall_frequency_hz = frequency_sums / reduced_widths
        time_bin = min(
            waterfall_time_bins - 1,
            (frame_index * waterfall_time_bins) // count,
        )
        waterfall_sum[time_bin] += reduced_power
        waterfall_counts[time_bin] += 1

    average_power = average_sum / count
    average_db = power_to_db(average_power)
    max_hold_db = power_to_db(max_hold)
    percentile_db = np.percentile(
        percentile_samples_db, settings.percentile, axis=0
    ).astype(np.float64)
    noise_floor_db = noise_sum_db / count
    occupancy_pct = occupied.astype(np.float64) * (100.0 / count)
    waterfall_power = waterfall_sum / np.maximum(waterfall_counts[:, None], 1)
    waterfall_db = power_to_db(waterfall_power).astype(np.float32)
    assert waterfall_frequency_hz is not None

    step = fft_step(settings.fft_size, settings.overlap_ratio)
    waterfall_time_s = (
        (np.arange(waterfall_time_bins, dtype=np.float64) + 0.5)
        * info.duration_seconds
        / waterfall_time_bins
    )
    low_hz = float(info.nominal_frequency_low_hz)
    high_hz = float(info.nominal_frequency_high_hz)
    center_hz = float(info.center_frequency_hz)
    edge_mask = (frequency_hz < low_hz + settings.edge_exclusion_hz) | (
        frequency_hz > high_hz - settings.edge_exclusion_hz
    )
    dc_mask = np.abs(frequency_hz - center_hz) <= settings.dc_exclusion_hz

    arrays = SpectrumArrays(
        frequency_hz=frequency_hz,
        average_db=average_db,
        max_hold_db=max_hold_db,
        percentile_db=percentile_db,
        noise_floor_db=noise_floor_db,
        occupancy_pct=occupancy_pct,
        waterfall_db=waterfall_db,
        waterfall_frequency_hz=waterfall_frequency_hz,
        waterfall_time_s=waterfall_time_s,
        edge_mask=edge_mask,
        dc_mask=dc_mask,
    )
    metrics = {
        "fft_count": count,
        "fft_size": settings.fft_size,
        "fft_step_frames": step,
        "frequency_resolution_hz": resolution_hz,
        "percentile_sampled_fft_count": len(percentile_indices),
        "waterfall_time_bins": waterfall_time_bins,
        "waterfall_frequency_bins": waterfall_frequency_bins,
        "noise_window_bins": bins_per_noise_window,
        "nominal_frequency_low_hz": low_hz,
        "nominal_frequency_high_hz": high_hz,
        "first_fft_bin_hz": float(frequency_hz[0]),
        "last_fft_bin_hz": float(frequency_hz[-1]),
    }
    return arrays, metrics


def _write_report(
    destination: Path,
    source: Path,
    info: dict[str, Any],
    settings: SpectrumSettings,
    metrics: dict[str, Any],
    elapsed: float,
    warnings: list[str],
) -> None:
    warning_lines = "\n".join(f"- {warning}" for warning in warnings) or "- None"
    report = f"""# Spectrum analysis

## Source

- Recording: `{source}`
- Center frequency: **{info['center_frequency_hz']:,} Hz**
- Center-frequency source: **{info['center_frequency_source']}**
- Sample rate: **{info['fmt']['sample_rate_hz']:,} samples/s**
- Duration: **{info['duration_seconds']:.6f} s**
- IQ order: **{info['iq_order']}**

## FFT

- FFT size: **{settings.fft_size:,}**
- Window: **{settings.window}**
- Overlap: **{settings.overlap_ratio:.3f}**
- FFT count: **{metrics['fft_count']:,}**
- FFT step: **{metrics['fft_step_frames']:,} frames**
- Frequency resolution: **{metrics['frequency_resolution_hz']:.6f} Hz/bin**
- Nominal coverage: **{metrics['nominal_frequency_low_hz'] / 1e6:.6f}–{metrics['nominal_frequency_high_hz'] / 1e6:.6f} MHz**
- First/last FFT-bin centers: **{metrics['first_fft_bin_hz'] / 1e6:.6f}–{metrics['last_fft_bin_hz'] / 1e6:.6f} MHz**

## Detection support

- Local noise window: **{settings.local_noise_window_hz:,.0f} Hz**
- Occupancy threshold: **noise floor + {settings.occupancy_threshold_db:.1f} dB**
- Edge regions flagged: **{settings.edge_exclusion_hz:,.0f} Hz per edge**
- DC region flagged: **±{settings.dc_exclusion_hz:,.0f} Hz**
- Percentile: **P{settings.percentile:g}** from **{metrics['percentile_sampled_fft_count']}** deterministic FFT frames
- Waterfall shape: **{metrics['waterfall_time_bins']} × {metrics['waterfall_frequency_bins']}**

## Runtime

- Elapsed: **{elapsed:.3f} s**
- Peak process RSS: **{_peak_rss_bytes() / (1024 ** 2):.1f} MiB**

## Warnings and provenance

{warning_lines}

The edge and DC regions are flagged in CSV outputs and shaded in plots. They are not silently removed.
"""
    (destination / "report.md").write_text(report, encoding="utf-8")


def run_spectrum(
    input_path: str | Path,
    output_dir: str | Path,
    *,
    settings: SpectrumSettings | None = None,
    assumed_iq_order: str = "IQ",
) -> dict[str, Any]:
    started = time.time()
    source = Path(input_path).expanduser().resolve()
    destination = Path(output_dir).expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)
    resolved = settings or SpectrumSettings()
    resolved.validate()

    info = inspect_wave_iq(source, assumed_iq_order=assumed_iq_order)
    reader = IQMemmapReader(info)
    arrays, metrics = _analyze(reader, resolved)
    info_dict = info.to_dict()

    _write_series_csv(
        destination / "average_spectrum.csv",
        arrays.frequency_hz,
        "average_psd_dbfs_per_hz",
        arrays.average_db,
        arrays.edge_mask,
        arrays.dc_mask,
    )
    _write_series_csv(
        destination / "percentile_spectrum.csv",
        arrays.frequency_hz,
        f"p{resolved.percentile:g}_psd_dbfs_per_hz",
        arrays.percentile_db,
        arrays.edge_mask,
        arrays.dc_mask,
    )
    _write_series_csv(
        destination / "noise_floor.csv",
        arrays.frequency_hz,
        "noise_floor_dbfs_per_hz",
        arrays.noise_floor_db,
        arrays.edge_mask,
        arrays.dc_mask,
    )
    _write_occupancy_csv(
        destination / "occupancy.csv",
        arrays.frequency_hz,
        arrays.occupancy_pct,
        arrays.edge_mask,
        arrays.dc_mask,
    )
    np.save(destination / "waterfall.npy", arrays.waterfall_db)
    np.savez(
        destination / "waterfall_axes.npz",
        frequency_hz=arrays.waterfall_frequency_hz,
        time_s=arrays.waterfall_time_s,
    )

    center_hz = float(info.center_frequency_hz)
    low_hz = float(info.nominal_frequency_low_hz)
    high_hz = float(info.nominal_frequency_high_hz)
    _plot_line(
        destination / "average_spectrum.png",
        arrays.frequency_hz,
        arrays.average_db,
        "Average spectrum",
        "PSD (dBFS/Hz, relative)",
        center_hz,
        low_hz,
        high_hz,
        resolved,
    )
    _plot_line(
        destination / "max_hold_spectrum.png",
        arrays.frequency_hz,
        arrays.max_hold_db,
        "Max-hold spectrum",
        "PSD (dBFS/Hz, relative)",
        center_hz,
        low_hz,
        high_hz,
        resolved,
    )
    _plot_waterfall(destination / "waterfall.png", arrays, "IQ recording waterfall")

    elapsed = time.time() - started
    warnings = [*info.warnings]
    if info.iq_order_confidence != "confirmed":
        warnings.append(
            "IQ order is assumed; compare a known carrier with SDRconnect before relying on mirrored frequency placement"
        )
    report_payload = {
        "tool": "dmr-iq-surveyor",
        "tool_version": __version__,
        "input_path": str(source),
        "output_dir": str(destination),
        "recording": info_dict,
        "settings": resolved.to_dict(),
        "metrics": metrics,
        "warnings": warnings,
        "elapsed_seconds": elapsed,
        "peak_rss_bytes": _peak_rss_bytes(),
        "python": platform.python_version(),
        "outputs": [
            "average_spectrum.csv",
            "average_spectrum.png",
            "max_hold_spectrum.png",
            "percentile_spectrum.csv",
            "waterfall.png",
            "waterfall.npy",
            "waterfall_axes.npz",
            "noise_floor.csv",
            "occupancy.csv",
            "spectrum_report.json",
            "report.md",
        ],
    }
    write_json(destination / "spectrum_report.json", report_payload)
    _write_report(destination, source, info_dict, resolved, metrics, elapsed, warnings)
    return {
        "summary": report_payload,
        "frequency_hz": arrays.frequency_hz,
        "average_db": arrays.average_db,
        "max_hold_db": arrays.max_hold_db,
    }


def _load_spectrum_batch_config(config_path: str | Path) -> dict[str, Any]:
    config = load_batch_config(config_path)
    raw = yaml.safe_load(Path(config["config_path"]).read_text(encoding="utf-8"))
    spectrum_raw = raw.get("spectrum") if isinstance(raw.get("spectrum"), dict) else {}
    config["spectrum"] = _settings_from_mapping(spectrum_raw)
    return config


def _write_batch_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fieldnames = list(rows[0]) if rows else []
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _plot_combined(
    path: Path,
    results: list[tuple[BatchRecording, dict[str, Any]]],
    field: str,
    title: str,
) -> None:
    plt.figure(figsize=(14, 6))
    for recording, result in results:
        label = recording.label or recording.recording_id
        plt.plot(
            result["frequency_hz"] / 1e6,
            result[field],
            linewidth=0.7,
            label=label,
        )
    plt.xlabel("Frequency (MHz)")
    plt.ylabel("PSD (dBFS/Hz, relative)")
    plt.title(title)
    plt.grid(alpha=0.2)
    plt.legend()
    plt.tight_layout()
    plt.savefig(path, dpi=150)
    plt.close()


def run_spectrum_batch(config_path: str | Path) -> dict[str, Any]:
    config = _load_spectrum_batch_config(config_path)
    output_root: Path = config["output_root"]
    output_root.mkdir(parents=True, exist_ok=True)
    settings: SpectrumSettings = config["spectrum"]
    iq_order = config["inspection"]["assumed_iq_order"]
    rows: list[dict[str, Any]] = []
    successes: list[tuple[BatchRecording, dict[str, Any]]] = []

    for recording in config["recordings"]:
        destination = output_root / "recordings" / recording.recording_id / "spectrum"
        try:
            result = run_spectrum(
                recording.path,
                destination,
                settings=settings,
                assumed_iq_order=iq_order,
            )
        except Exception as exc:
            rows.append(
                {
                    "recording_id": recording.recording_id,
                    "label": recording.label or "",
                    "status": "failed",
                    "error": f"{type(exc).__name__}: {exc}",
                    "duration_seconds": "",
                    "fft_count": "",
                    "frequency_resolution_hz": "",
                    "elapsed_seconds": "",
                    "peak_rss_bytes": "",
                    "output_dir": str(destination),
                }
            )
            continue
        summary = result["summary"]
        successes.append((recording, result))
        rows.append(
            {
                "recording_id": recording.recording_id,
                "label": recording.label or "",
                "status": "ok",
                "error": "",
                "duration_seconds": summary["recording"]["duration_seconds"],
                "fft_count": summary["metrics"]["fft_count"],
                "frequency_resolution_hz": summary["metrics"]["frequency_resolution_hz"],
                "elapsed_seconds": summary["elapsed_seconds"],
                "peak_rss_bytes": summary["peak_rss_bytes"],
                "output_dir": str(destination),
            }
        )

    _write_batch_csv(output_root / "spectrum_batch_summary.csv", rows)
    if successes:
        _plot_combined(
            output_root / "combined_average_spectrum.png",
            successes,
            "average_db",
            "Average spectrum comparison",
        )
        _plot_combined(
            output_root / "combined_max_hold_spectrum.png",
            successes,
            "max_hold_db",
            "Max-hold spectrum comparison",
        )

    batch_payload = {
        "project_name": config["project_name"],
        "config_path": str(config["config_path"]),
        "output_root": str(output_root),
        "settings": settings.to_dict(),
        "successful_recordings": sum(row["status"] == "ok" for row in rows),
        "failed_recordings": sum(row["status"] != "ok" for row in rows),
        "rows": rows,
    }
    write_json(output_root / "spectrum_batch_summary.json", batch_payload)
    table = [
        "| Recording | Status | FFTs | Resolution Hz | Elapsed s |",
        "|---|---:|---:|---:|---:|",
    ]
    for row in rows:
        table.append(
            f"| `{row['recording_id']}` | {row['status']} | {row['fft_count'] or '-'} | "
            f"{row['frequency_resolution_hz'] or '-'} | {row['elapsed_seconds'] or '-'} |"
        )
    report = f"""# Batch spectrum analysis — {config['project_name']}

- Successful recordings: **{batch_payload['successful_recordings']}**
- Failed recordings: **{batch_payload['failed_recordings']}**
- Recordings were analyzed independently and were not concatenated.

{chr(10).join(table)}

Open each `recordings/<id>/spectrum/report.md`, then inspect the combined average and max-hold plots.
"""
    (output_root / "spectrum_batch_report.md").write_text(report, encoding="utf-8")
    return batch_payload
