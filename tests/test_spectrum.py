from __future__ import annotations

import csv
import struct
from pathlib import Path

import numpy as np
import yaml

from dmr_iq_surveyor.spectrum import SpectrumSettings, run_spectrum, run_spectrum_batch
from dmr_iq_surveyor.spectrum.core import (
    fft_frame_count,
    frequency_axis_hz,
    iter_fft_starts,
    periodogram_power_density,
)


def _chunk(chunk_id: bytes, payload: bytes) -> bytes:
    padding = b"\x00" if len(payload) & 1 else b""
    return chunk_id + struct.pack("<I", len(payload)) + payload + padding


def _system_time() -> tuple[int, ...]:
    return (2026, 7, 0, 13, 12, 0, 0, 0)


def _write_tone_riff(
    path: Path,
    *,
    sample_rate: int = 1_000_000,
    center_hz: int = 100_000_000,
    offset_hz: float = 125_000.0,
    frame_count: int = 16_384,
) -> None:
    time_values = np.arange(frame_count, dtype=np.float64) / sample_rate
    tone = 0.5 * np.exp(2j * np.pi * offset_hz * time_values)
    samples = np.column_stack((tone.real, tone.imag))
    pcm = np.round(samples * 32767.0).astype("<i2")
    fmt = struct.pack("<HHIIHH", 1, 2, sample_rate, sample_rate * 4, 4, 16)
    auxi_values = (
        *_system_time(),
        *_system_time(),
        center_hz,
        sample_rate,
        0,
        sample_rate,
        0,
        0,
        32767,
        0,
        0,
        b"\x00" * 96,
    )
    auxi = struct.pack("<8H8H9I96s", *auxi_values)
    body = _chunk(b"fmt ", fmt) + _chunk(b"auxi", auxi) + _chunk(b"data", pcm.tobytes())
    path.write_bytes(b"RIFF" + struct.pack("<I", 4 + len(body)) + b"WAVE" + body)


def test_frequency_axis_and_overlap_logic() -> None:
    axis = frequency_axis_hz(100_000_000.0, 1_000_000.0, 8)
    np.testing.assert_allclose(
        axis,
        [
            99_500_000,
            99_625_000,
            99_750_000,
            99_875_000,
            100_000_000,
            100_125_000,
            100_250_000,
            100_375_000,
        ],
    )
    assert fft_frame_count(20, 8, 0.5) == 4
    assert list(iter_fft_starts(20, 8, 0.5)) == [0, 4, 8, 12]


def test_periodogram_places_complex_tone_in_expected_bin() -> None:
    sample_rate = 1_000_000
    fft_size = 1024
    offset_hz = 125_000
    time_values = np.arange(fft_size, dtype=np.float64) / sample_rate
    samples = np.exp(2j * np.pi * offset_hz * time_values)
    window = np.ones(fft_size, dtype=np.float64)
    power = periodogram_power_density(samples, window, sample_rate)
    axis = frequency_axis_hz(100_000_000, sample_rate, fft_size)
    assert abs(axis[int(np.argmax(power))] - 100_125_000) < 1.0


def test_run_spectrum_writes_artifacts_and_finds_tone(tmp_path: Path) -> None:
    source = tmp_path / "tone.wav"
    _write_tone_riff(source)
    output = tmp_path / "spectrum"
    settings = SpectrumSettings(
        fft_size=1024,
        overlap_ratio=0.5,
        waterfall_time_bins=16,
        waterfall_frequency_bins=128,
        percentile_max_frames=16,
        local_noise_window_hz=100_000,
    )

    result = run_spectrum(source, output, settings=settings)

    for filename in [
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
    ]:
        assert (output / filename).is_file()

    peak_frequency = result["frequency_hz"][int(np.argmax(result["average_db"]))]
    assert abs(peak_frequency - 100_125_000) < 1_000
    waterfall = np.load(output / "waterfall.npy")
    assert waterfall.shape == (16, 128)

    with (output / "occupancy.csv").open(encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 1024
    assert {"edge_excluded", "dc_excluded"}.issubset(rows[0])


def test_spectrum_batch_keeps_recordings_independent(tmp_path: Path) -> None:
    first = tmp_path / "first.wav"
    second = tmp_path / "second.wav"
    _write_tone_riff(first, offset_hz=125_000)
    _write_tone_riff(second, offset_hz=-125_000)
    output = tmp_path / "batch"
    config = tmp_path / "batch.yaml"
    config.write_text(
        yaml.safe_dump(
            {
                "project": {"name": "test", "output_root": str(output)},
                "inspection": {"assumed_iq_order": "IQ"},
                "spectrum": {
                    "fft_size": 1024,
                    "waterfall_time_bins": 8,
                    "waterfall_frequency_bins": 64,
                    "percentile_max_frames": 8,
                },
                "recordings": [
                    {"id": "first", "path": str(first)},
                    {"id": "second", "path": str(second)},
                ],
            }
        ),
        encoding="utf-8",
    )

    result = run_spectrum_batch(config)

    assert result["successful_recordings"] == 2
    assert result["failed_recordings"] == 0
    assert (output / "combined_average_spectrum.png").is_file()
    assert (output / "combined_max_hold_spectrum.png").is_file()
    assert (output / "recordings" / "first" / "spectrum" / "report.md").is_file()
    assert (output / "recordings" / "second" / "spectrum" / "report.md").is_file()
