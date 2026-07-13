from __future__ import annotations

import csv
import hashlib
import json
import math
import platform
import resource
import sys
import time
from dataclasses import asdict
from importlib import metadata as importlib_metadata
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

from dmr_iq_surveyor import __version__
from dmr_iq_surveyor.iq.metadata import inspect_wave_iq
from dmr_iq_surveyor.iq.reader import IQMemmapReader
from dmr_iq_surveyor.models import RecordingInfo


def _json_default(value: Any) -> Any:
    if isinstance(value, (np.integer, np.floating)):
        return value.item()
    if isinstance(value, np.ndarray):
        return value.tolist()
    raise TypeError(f"Cannot serialize {type(value)!r}")


def write_json(path: Path, payload: Any) -> None:
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, default=_json_default) + "\n",
        encoding="utf-8",
    )


def sha256_file(path: Path, block_size: int = 8 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(block_size):
            digest.update(block)
    return digest.hexdigest()


def _channel_stats(values: np.ndarray) -> dict[str, float | int]:
    if values.size == 0:
        return {"count": 0}
    finite = values[np.isfinite(values)]
    if finite.size == 0:
        return {"count": int(values.size), "finite_count": 0}
    absolute = np.abs(finite)
    return {
        "count": int(values.size),
        "finite_count": int(finite.size),
        "mean": float(np.mean(finite)),
        "std": float(np.std(finite)),
        "rms": float(np.sqrt(np.mean(finite * finite))),
        "min": float(np.min(finite)),
        "max": float(np.max(finite)),
        "peak_abs": float(np.max(absolute)),
        "zero_fraction": float(np.mean(finite == 0)),
        "near_clip_fraction": float(np.mean(absolute >= 0.999)),
    }


def _window_positions(total_frames: int, window_frames: int) -> list[tuple[str, int, int]]:
    count = min(total_frames, max(1, window_frames))
    if total_frames <= count:
        return [("entire_recording", 0, total_frames)]
    starts = {
        "beginning": 0,
        "middle": max(0, total_frames // 2 - count // 2),
        "end": max(0, total_frames - count),
    }
    return [(name, start, count) for name, start in starts.items()]


def calculate_sample_statistics(reader: IQMemmapReader, window_frames: int) -> dict[str, Any]:
    windows: list[dict[str, Any]] = []
    aggregate_i: list[np.ndarray] = []
    aggregate_q: list[np.ndarray] = []

    for name, start, count in _window_positions(reader.frame_count, window_frames):
        channels = reader.read_channels(start, count, normalize=True)
        i_index, q_index = (0, 1) if reader.info.iq_order == "IQ" else (1, 0)
        i_values = channels[:, i_index]
        q_values = channels[:, q_index]
        aggregate_i.append(i_values)
        aggregate_q.append(q_values)
        complex_values = i_values + 1j * q_values
        correlation = float(np.corrcoef(i_values, q_values)[0, 1]) if len(i_values) > 1 else 0.0
        windows.append(
            {
                "name": name,
                "start_frame": start,
                "frame_count": int(len(channels)),
                "start_seconds": start / reader.info.fmt.sample_rate_hz,
                "i": _channel_stats(i_values),
                "q": _channel_stats(q_values),
                "iq_correlation": correlation if math.isfinite(correlation) else None,
                "complex_magnitude": _channel_stats(np.abs(complex_values)),
            }
        )

    all_i = np.concatenate(aggregate_i) if aggregate_i else np.array([], dtype=np.float32)
    all_q = np.concatenate(aggregate_q) if aggregate_q else np.array([], dtype=np.float32)
    warnings: list[str] = []
    i_stats = _channel_stats(all_i)
    q_stats = _channel_stats(all_q)

    if i_stats.get("near_clip_fraction", 0.0) > 0.001 or q_stats.get("near_clip_fraction", 0.0) > 0.001:
        warnings.append("Sample windows contain a measurable fraction of near-clipped values")
    if i_stats.get("std", 0.0) == 0.0 or q_stats.get("std", 0.0) == 0.0:
        warnings.append("At least one IQ channel has zero variance in the sampled windows")
    if i_stats.get("zero_fraction", 0.0) > 0.5 or q_stats.get("zero_fraction", 0.0) > 0.5:
        warnings.append("At least one IQ channel is more than 50% zero in the sampled windows")
    i_std = float(i_stats.get("std", 0.0))
    q_std = float(q_stats.get("std", 0.0))
    if min(i_std, q_std) > 0 and max(i_std, q_std) / min(i_std, q_std) > 2.0:
        warnings.append("I and Q standard deviations differ by more than a factor of two")

    return {
        "method": "three_bounded_windows_not_full_file",
        "requested_window_frames": window_frames,
        "total_sampled_frames": int(sum(len(v) for v in aggregate_i)),
        "aggregate": {
            "i": i_stats,
            "q": q_stats,
            "iq_correlation": float(np.corrcoef(all_i, all_q)[0, 1]) if len(all_i) > 1 else None,
        },
        "windows": windows,
        "warnings": warnings,
    }


def create_diagnostic_plots(reader: IQMemmapReader, output_dir: Path, plot_frames: int) -> list[str]:
    count = min(reader.frame_count, max(1, plot_frames))
    start = max(0, reader.frame_count // 2 - count // 2)
    complex_values = reader.read_complex(start, count)
    sample_rate = reader.info.fmt.sample_rate_hz
    time_ms = np.arange(len(complex_values), dtype=np.float64) / sample_rate * 1000.0

    time_path = output_dir / "diagnostic_time.png"
    plt.figure(figsize=(12, 5))
    plt.plot(time_ms, complex_values.real, linewidth=0.6, label="I")
    plt.plot(time_ms, complex_values.imag, linewidth=0.6, label="Q", alpha=0.75)
    plt.xlabel("Time (ms)")
    plt.ylabel("Normalized amplitude")
    plt.title(f"IQ time samples near recording midpoint ({len(complex_values):,} frames)")
    plt.legend()
    plt.tight_layout()
    plt.savefig(time_path, dpi=150)
    plt.close()

    constellation_path = output_dir / "diagnostic_iq.png"
    stride = max(1, len(complex_values) // 20000)
    shown = complex_values[::stride]
    plt.figure(figsize=(7, 7))
    plt.scatter(shown.real, shown.imag, s=1, alpha=0.25)
    plt.xlabel("I")
    plt.ylabel("Q")
    plt.title("Raw IQ sample distribution")
    plt.axis("equal")
    plt.tight_layout()
    plt.savefig(constellation_path, dpi=150)
    plt.close()

    return [time_path.name, constellation_path.name]


def write_chunk_map(info: RecordingInfo, path: Path) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["chunk_id", "header_offset", "data_offset", "declared_size", "effective_size", "truncated"],
        )
        writer.writeheader()
        for chunk in info.chunks:
            writer.writerow(asdict(chunk))


def _format_hz(value: float | int | None) -> str:
    if value is None:
        return "Unknown"
    return f"{value:,.0f} Hz ({value / 1e6:.6f} MHz)"


def write_report(
    info: RecordingInfo,
    statistics: dict[str, Any],
    output_dir: Path,
    sha256: str | None,
    plot_names: list[str],
) -> None:
    warnings = [*info.warnings, *statistics.get("warnings", [])]
    warning_lines = "\n".join(f"- {item}" for item in warnings) or "- None detected"
    chunk_lines = "\n".join(
        f"- `{chunk.chunk_id}`: offset {chunk.data_offset:,}, size {chunk.effective_size:,} bytes"
        for chunk in info.chunks
    )
    report = f"""# SDRconnect IQ Recording Inspection

## Source

- File: `{info.path}`
- File size: {info.file_size_bytes:,} bytes
- SHA-256: `{sha256 or 'skipped'}`

## Container and samples

- Container: **{info.container}/{info.wave_format}**
- WAVE format code: `{info.fmt.format_code}` (effective `{info.fmt.effective_format_code}`)
- Channels: **{info.fmt.channels}**
- Sample rate: **{info.fmt.sample_rate_hz:,} samples/s**
- Bits per sample: **{info.fmt.bits_per_sample}**
- Encoding: **{info.sample_encoding}**
- Block alignment: **{info.fmt.block_align} bytes/frame**
- IQ order used: **{info.iq_order}**
- IQ-order confidence: `{info.iq_order_confidence}`

## Recording geometry

- Center frequency: **{_format_hz(info.center_frequency_hz)}**
- Center-frequency source: **{info.center_frequency_source}**
- Nominal low edge: **{_format_hz(info.nominal_frequency_low_hz)}**
- Nominal high edge: **{_format_hz(info.nominal_frequency_high_hz)}**
- Frames: **{info.frame_count:,}**
- Duration: **{info.duration_seconds:.6f} s**
- Data offset: **{info.data_offset_bytes:,} bytes**
- Available IQ data: **{info.data_available_size_bytes:,} bytes**

## Metadata chunks

{chunk_lines}

## Sample-window checks

- Method: `{statistics['method']}`
- Sampled frames: **{statistics['total_sampled_frames']:,}**
- Aggregate I standard deviation: `{statistics['aggregate']['i'].get('std')}`
- Aggregate Q standard deviation: `{statistics['aggregate']['q'].get('std')}`
- Aggregate I near-clip fraction: `{statistics['aggregate']['i'].get('near_clip_fraction')}`
- Aggregate Q near-clip fraction: `{statistics['aggregate']['q'].get('near_clip_fraction')}`
- IQ correlation: `{statistics['aggregate'].get('iq_correlation')}`

## Warnings

{warning_lines}

## Diagnostic files

""" + "\n".join(f"- `{name}`" for name in plot_names) + "\n"
    (output_dir / "report.md").write_text(report, encoding="utf-8")


def _package_versions() -> dict[str, str]:
    result: dict[str, str] = {}
    for package in ["numpy", "matplotlib", "typer", "rich", "PyYAML"]:
        try:
            result[package] = importlib_metadata.version(package)
        except importlib_metadata.PackageNotFoundError:
            result[package] = "not-installed"
    return result


def run_inspection(
    input_path: str | Path,
    output_dir: str | Path,
    *,
    assumed_iq_order: str = "IQ",
    statistics_window_frames: int = 250_000,
    diagnostic_plot_frames: int = 20_000,
    compute_sha256: bool = True,
) -> dict[str, Any]:
    started = time.time()
    source = Path(input_path).expanduser().resolve()
    destination = Path(output_dir).expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)

    info = inspect_wave_iq(source, assumed_iq_order=assumed_iq_order)
    reader = IQMemmapReader(info)
    statistics = calculate_sample_statistics(reader, statistics_window_frames)
    digest = sha256_file(source) if compute_sha256 else None
    plots = create_diagnostic_plots(reader, destination, diagnostic_plot_frames)

    write_json(destination / "recording_info.json", info.to_dict())
    write_json(destination / "sample_statistics.json", statistics)
    write_chunk_map(info, destination / "chunk_map.csv")
    write_report(info, statistics, destination, digest, plots)

    elapsed = time.time() - started
    peak_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    peak_rss_bytes = int(peak_rss * 1024 if sys.platform != "darwin" else peak_rss)
    manifest = {
        "tool": "dmr-iq-surveyor",
        "tool_version": __version__,
        "input_path": str(source),
        "output_dir": str(destination),
        "sha256": digest,
        "started_unix": started,
        "elapsed_seconds": elapsed,
        "peak_rss_bytes": peak_rss_bytes,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "package_versions": _package_versions(),
        "parameters": {
            "assumed_iq_order": assumed_iq_order,
            "statistics_window_frames": statistics_window_frames,
            "diagnostic_plot_frames": diagnostic_plot_frames,
            "compute_sha256": compute_sha256,
        },
        "outputs": [
            "recording_info.json",
            "sample_statistics.json",
            "chunk_map.csv",
            "diagnostic_time.png",
            "diagnostic_iq.png",
            "report.md",
        ],
    }
    write_json(destination / "manifest.json", manifest)
    return {"recording": info.to_dict(), "statistics": statistics, "manifest": manifest}
