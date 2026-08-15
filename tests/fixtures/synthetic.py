"""Synthetic wideband IQ WAV fixtures for Phase 6 survey tests.

No real IQ recordings are used by the unit test suite. This module
generates small, deterministic RIFF/WAVE files with known tones at known
frequency offsets, optionally present only during specific time ranges (to
exercise persistence vs. occupancy independently), written directly to disk
in bounded chunks so tests never hold a large recording in memory.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np


def _system_time(moment: datetime) -> tuple[int, ...]:
    return (
        moment.year,
        moment.month,
        0,
        moment.day,
        moment.hour,
        moment.minute,
        moment.second,
        0,
    )


def _auxi_payload(center_hz: int, sample_rate_hz: int, start: datetime, stop: datetime) -> bytes:
    values = (
        *_system_time(start),
        *_system_time(stop),
        center_hz,
        sample_rate_hz,
        0,
        sample_rate_hz,
        0,
        0,
        32767,
        0,
        0,
        b"\x00" * 96,
    )
    return struct.pack("<8H8H9I96s", *values)


def _chunk(chunk_id: bytes, payload: bytes) -> bytes:
    padding = b"\x00" if len(payload) & 1 else b""
    return chunk_id + struct.pack("<I", len(payload)) + payload + padding


@dataclass(slots=True)
class SyntheticTone:
    offset_hz: float
    amplitude: float = 0.2
    # Time ranges (seconds, relative to recording start) during which the
    # tone is present. Defaults to always-on. Use disjoint short ranges to
    # test persistence (segments where it's independently detected) versus
    # occupancy (fraction of analyzed time it was actually present).
    active_ranges: list[tuple[float, float]] = field(default_factory=lambda: [(0.0, 1e9)])
    # A little bandwidth so the detector sees a real width, not a pure tone.
    modulation_bandwidth_hz: float = 2000.0

    def is_active(self, start_seconds: float, end_seconds: float) -> bool:
        return any(
            start_seconds < active_end and end_seconds > active_start
            for active_start, active_end in self.active_ranges
        )


def write_synthetic_iq_wav(
    path: Path,
    *,
    sample_rate_hz: int,
    center_frequency_hz: int,
    duration_seconds: float,
    tones: list[SyntheticTone],
    noise_std: float = 0.02,
    capture_start_utc: datetime | None = None,
    seed: int = 0,
    chunk_seconds: float = 0.5,
) -> None:
    """Write a RIFF/WAVE int16 IQ recording with SDRplay-style auxi metadata."""
    start = capture_start_utc or datetime(2026, 8, 1, 12, 0, 0, tzinfo=UTC)
    total_frames = round(duration_seconds * sample_rate_hz)
    stop = start
    rng = np.random.default_rng(seed)
    chunk_frames = max(1, round(chunk_seconds * sample_rate_hz))

    with path.open("wb") as handle:
        # Placeholder RIFF header; sizes are patched once the true data size
        # is known, avoiding a second full-file pass.
        handle.write(b"RIFF\x00\x00\x00\x00WAVE")
        fmt_payload = struct.pack("<HHIIHH", 1, 2, sample_rate_hz, sample_rate_hz * 4, 4, 16)
        handle.write(_chunk(b"fmt ", fmt_payload))
        handle.write(_chunk(b"auxi", _auxi_payload(center_frequency_hz, sample_rate_hz, start, stop)))
        handle.write(b"data" + struct.pack("<I", total_frames * 4))
        data_start = handle.tell()

        written = 0
        while written < total_frames:
            count = min(chunk_frames, total_frames - written)
            t = (np.arange(count, dtype=np.float64) + written) / sample_rate_hz
            complex_samples = (
                rng.normal(scale=noise_std, size=count)
                + 1j * rng.normal(scale=noise_std, size=count)
            ).astype(np.complex128)
            for tone in tones:
                segment_start = written / sample_rate_hz
                segment_end = (written + count) / sample_rate_hz
                if not tone.is_active(segment_start, segment_end):
                    continue
                mask = np.zeros(count, dtype=bool)
                for active_start, active_end in tone.active_ranges:
                    mask |= (t >= active_start) & (t < active_end)
                if not np.any(mask):
                    continue
                wobble = tone.modulation_bandwidth_hz / 2.0
                instantaneous_offset = tone.offset_hz + wobble * np.sin(
                    2.0 * np.pi * 37.0 * t
                )
                phase = 2.0 * np.pi * np.cumsum(instantaneous_offset) / sample_rate_hz
                carrier = tone.amplitude * np.exp(1j * phase)
                complex_samples += np.where(mask, carrier, 0.0)
            scale = 32767.0 * 0.7
            i_values = np.clip(np.real(complex_samples) * scale, -32768, 32767).astype("<i2")
            q_values = np.clip(np.imag(complex_samples) * scale, -32768, 32767).astype("<i2")
            interleaved = np.empty(count * 2, dtype="<i2")
            interleaved[0::2] = i_values
            interleaved[1::2] = q_values
            handle.write(interleaved.tobytes())
            written += count

        data_end = handle.tell()
        if (data_end - data_start) & 1:
            handle.write(b"\x00")
        file_end = handle.tell()
        riff_size = file_end - 8
        handle.seek(4)
        handle.write(struct.pack("<I", riff_size))

    # Rewrite the auxi chunk's stop time now that duration is known. Cheap:
    # only a fixed small header region is touched, not the sample data.
    stop = start + timedelta(seconds=duration_seconds)
    with path.open("r+b") as handle:
        handle.seek(12)
        while True:
            header = handle.read(8)
            if len(header) < 8:
                break
            chunk_id, size = struct.unpack("<4sI", header)
            if chunk_id == b"auxi":
                handle.write(_auxi_payload(center_frequency_hz, sample_rate_hz, start, stop))
                break
            handle.seek(size + (size & 1), 1)


__all__ = ["SyntheticTone", "write_synthetic_iq_wav"]
