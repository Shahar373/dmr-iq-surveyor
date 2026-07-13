from __future__ import annotations

import struct
from pathlib import Path

import numpy as np

from dmr_iq_surveyor.iq.metadata import inspect_wave_iq
from dmr_iq_surveyor.iq.reader import IQMemmapReader


def _system_time(year: int, month: int, day: int, hour: int, minute: int, second: int):
    return (year, month, 0, day, hour, minute, second, 0)


def _auxi(center_hz: int, sample_rate: int) -> bytes:
    values = (
        *_system_time(2026, 7, 13, 12, 0, 0),
        *_system_time(2026, 7, 13, 12, 0, 1),
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
    return struct.pack("<8H8H9I96s", *values)


def _chunk(chunk_id: bytes, payload: bytes) -> bytes:
    padding = b"\x00" if len(payload) & 1 else b""
    return chunk_id + struct.pack("<I", len(payload)) + payload + padding


def create_riff(
    path: Path, *, rf64: bool, include_auxi: bool = True
) -> tuple[np.ndarray, int, int]:
    sample_rate = 1_000_000
    center_hz = 165_000_000
    samples = np.array(
        [[0, 0], [1000, -1000], [32767, -32768], [1234, 5678]], dtype="<i2"
    )
    data = samples.tobytes()
    fmt = struct.pack("<HHIIHH", 1, 2, sample_rate, sample_rate * 4, 4, 16)
    fmt_chunk = _chunk(b"fmt ", fmt)
    auxi_chunk = _chunk(b"auxi", _auxi(center_hz, sample_rate)) if include_auxi else b""

    if rf64:
        data_header = b"data" + struct.pack("<I", 0xFFFFFFFF)
        provisional_ds64 = _chunk(b"ds64", struct.pack("<QQQI", 0, len(data), len(samples), 0))
        body = provisional_ds64 + fmt_chunk + auxi_chunk + data_header + data
        full_size = 12 + len(body)
        ds64 = _chunk(b"ds64", struct.pack("<QQQI", full_size - 8, len(data), len(samples), 0))
        body = ds64 + fmt_chunk + auxi_chunk + data_header + data
        content = b"RF64" + struct.pack("<I", 0xFFFFFFFF) + b"WAVE" + body
    else:
        data_chunk = _chunk(b"data", data)
        body = fmt_chunk + auxi_chunk + data_chunk
        content = b"RIFF" + struct.pack("<I", 4 + len(body)) + b"WAVE" + body

    path.write_bytes(content)
    return samples, sample_rate, center_hz


def test_parse_sdrconnect_style_riff(tmp_path: Path):
    source = tmp_path / "sample.wav"
    samples, sample_rate, center_hz = create_riff(source, rf64=False)
    info = inspect_wave_iq(source)

    assert info.container == "RIFF"
    assert info.fmt.sample_rate_hz == sample_rate
    assert info.center_frequency_hz == center_hz
    assert info.center_frequency_source == "auxi"
    assert info.frame_count == len(samples)
    assert info.duration_seconds == len(samples) / sample_rate
    assert info.auxi is not None
    assert info.auxi.layout == "sdrplay-164"


def test_parse_rf64_and_memmap_samples(tmp_path: Path):
    source = tmp_path / "sample_rf64.wav"
    samples, sample_rate, center_hz = create_riff(source, rf64=True)
    info = inspect_wave_iq(source)
    reader = IQMemmapReader(info)
    channels = reader.read_channels(0, len(samples), normalize=False)

    assert info.container == "RF64"
    assert info.ds64 is not None
    assert info.ds64.data_size == samples.nbytes
    assert info.ds64.sample_count == len(samples)
    assert info.fmt.sample_rate_hz == sample_rate
    assert info.center_frequency_hz == center_hz
    assert info.center_frequency_source == "auxi"
    np.testing.assert_array_equal(channels, samples)


def test_nominal_frequency_span(tmp_path: Path):
    source = tmp_path / "span.wav"
    _, sample_rate, center_hz = create_riff(source, rf64=True)
    info = inspect_wave_iq(source)

    assert info.nominal_frequency_low_hz == center_hz - sample_rate / 2
    assert info.nominal_frequency_high_hz == center_hz + sample_rate / 2


def test_center_frequency_falls_back_to_sdrconnect_filename(tmp_path: Path):
    source = tmp_path / "SDRconnect_IQ_20260713_150256_163671500HZ.wav"
    _, sample_rate, _ = create_riff(source, rf64=True, include_auxi=False)
    info = inspect_wave_iq(source)

    assert info.center_frequency_hz == 163_671_500
    assert info.center_frequency_source == "filename"
    assert info.nominal_frequency_low_hz == 163_671_500 - sample_rate / 2
    assert any("derived from the filename" in warning for warning in info.warnings)


def test_center_frequency_can_remain_missing_without_crashing_parser(tmp_path: Path):
    source = tmp_path / "recording_without_frequency.wav"
    create_riff(source, rf64=False, include_auxi=False)
    info = inspect_wave_iq(source)

    assert info.center_frequency_hz is None
    assert info.center_frequency_source == "missing"
    assert info.nominal_frequency_low_hz is None
    assert info.nominal_frequency_high_hz is None
