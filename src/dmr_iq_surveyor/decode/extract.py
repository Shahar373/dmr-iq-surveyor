from __future__ import annotations

import json
import platform
import resource
import sys
import time
import wave
from pathlib import Path
from typing import Any, Iterator

import numpy as np

from dmr_iq_surveyor.decode.core import (
    ExtractionSettings,
    normalize_pcm16,
    process_complex_chunks,
)
from dmr_iq_surveyor.decode.profiles import extraction_profile
from dmr_iq_surveyor.iq.metadata import inspect_wave_iq
from dmr_iq_surveyor.iq.reader import IQMemmapReader


def _peak_rss_bytes() -> int:
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(value * 1024 if sys.platform != "darwin" else value)


def write_pcm16_wav(
    path: str | Path,
    samples: np.ndarray,
    sample_rate_hz: int,
) -> None:
    destination = Path(path)
    with wave.open(str(destination), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(int(sample_rate_hz))
        handle.writeframes(np.asarray(samples, dtype="<i2").tobytes())


def _reader_chunks(
    reader: IQMemmapReader,
    chunk_frames: int,
) -> Iterator[np.ndarray]:
    for start in range(0, reader.frame_count, chunk_frames):
        yield reader.read_complex(
            start,
            min(chunk_frames, reader.frame_count - start),
        )


def run_channel_extraction(
    input_path: str | Path,
    output_dir: str | Path,
    *,
    candidate_frequency_hz: float,
    settings: ExtractionSettings | None = None,
    profile_name: str | None = None,
    assumed_iq_order: str = "IQ",
    candidate_id: str | None = None,
    recording_id: str | None = None,
    capture_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    started = time.time()
    source = Path(input_path).expanduser().resolve()
    destination = Path(output_dir).expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)
    if settings is not None and profile_name is not None:
        raise ValueError("Use either explicit extraction settings or profile_name, not both")
    info = inspect_wave_iq(
        source,
        assumed_iq_order=assumed_iq_order,
    )
    if info.center_frequency_hz is None:
        raise ValueError(
            "Center frequency is required for channel extraction"
        )
    input_rate = int(info.fmt.sample_rate_hz)
    resolved_profile = profile_name or ("custom" if settings is not None else "10m")
    resolved = settings or extraction_profile(resolved_profile, input_rate)
    resolved.validate(input_rate)
    offset_hz = (
        float(candidate_frequency_hz)
        - float(info.center_frequency_hz)
    )
    if abs(offset_hz) >= input_rate / 2:
        raise ValueError(
            "Candidate frequency lies outside the recording passband"
        )

    reader = IQMemmapReader(info)
    discriminator, metrics, preview = process_complex_chunks(
        _reader_chunks(reader, resolved.chunk_frames),
        input_sample_rate_hz=input_rate,
        frequency_offset_hz=offset_hz,
        settings=resolved,
    )
    pcm, normalization = normalize_pcm16(
        discriminator,
        resolved.normalization_percentile,
        resolved.output_peak_fraction,
    )
    wav_path = destination / "discriminator.wav"
    write_pcm16_wav(
        wav_path,
        pcm,
        resolved.output_rate_hz,
    )
    np.savez_compressed(
        destination / "baseband_preview.npz",
        samples=preview,
        sample_rate_hz=metrics["intermediate_rate_hz"],
        candidate_frequency_hz=float(candidate_frequency_hz),
        recording_center_frequency_hz=float(
            info.center_frequency_hz
        ),
        iq_order=assumed_iq_order,
    )
    elapsed = time.time() - started
    output_duration = len(pcm) / resolved.output_rate_hz
    metadata = dict(capture_metadata or {})
    report = {
        "tool": "dmr-iq-surveyor",
        "input_path": str(source),
        "output_dir": str(destination),
        "candidate_id": candidate_id,
        "recording_id": recording_id,
        "candidate_frequency_hz": float(candidate_frequency_hz),
        "recording_center_frequency_hz": int(
            info.center_frequency_hz
        ),
        "frequency_offset_hz": offset_hz,
        "iq_order": assumed_iq_order,
        "iq_order_confidence": info.iq_order_confidence,
        "input_sample_rate_hz": input_rate,
        "input_duration_seconds": info.duration_seconds,
        "extraction_profile": resolved_profile,
        "output_sample_rate_hz": resolved.output_rate_hz,
        "output_duration_seconds": output_duration,
        "wav_path": str(wav_path),
        "settings": resolved.to_dict(),
        "metrics": metrics,
        "normalization": normalization,
        "capture_metadata": metadata,
        "warnings": list(info.warnings),
        "elapsed_seconds": elapsed,
        "peak_rss_bytes": _peak_rss_bytes(),
        "python": platform.python_version(),
    }
    (destination / "extraction_report.json").write_text(
        json.dumps(report, indent=2),
        encoding="utf-8",
    )
    warning_lines = (
        "\n".join(
            f"- {value}" for value in report["warnings"]
        )
        or "- None"
    )
    metadata_lines = (
        "\n".join(f"- {key}: **{value}**" for key, value in sorted(metadata.items()))
        or "- None"
    )
    markdown = f"""# Narrowband channel extraction

- Candidate: **{candidate_id or '-'}**
- Recording: **{recording_id or source.name}**
- Candidate frequency: **{candidate_frequency_hz / 1e6:.6f} MHz**
- Recording center: **{info.center_frequency_hz / 1e6:.6f} MHz**
- Mixer offset: **{offset_hz:,.3f} Hz**
- IQ order: **{assumed_iq_order}** ({info.iq_order_confidence})
- Input: **{input_rate:,} complex samples/s**, **{info.duration_seconds:.6f} s**
- Extraction profile: **{resolved_profile}**
- Output: **{resolved.output_rate_hz:,} Hz mono PCM16**, **{output_duration:.6f} s**
- Peak-safe limiter applied: **{normalization['limiter_applied']}**
- Samples that would clip without peak cap: **{normalization['would_clip_without_limiter']}**
- Clipped output samples: **{normalization['clipped_samples']}**
- Output peak: **{normalization['peak_pcm']} / 32767**
- Elapsed: **{elapsed:.3f} s**
- Peak RSS: **{_peak_rss_bytes() / (1024 ** 2):.1f} MiB**

## Capture metadata

{metadata_lines}

## Warnings

{warning_lines}
"""
    (destination / "extraction_report.md").write_text(
        markdown,
        encoding="utf-8",
    )
    return report
