from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from dmr_iq_surveyor.capture.wav_writer import WaveIQWriter, WaveIQWriterSettings
from dmr_iq_surveyor.iq.metadata import inspect_wave_iq
from dmr_iq_surveyor.iq.reader import IQMemmapReader

SAMPLE_RATE_HZ = 200_000
CENTER_HZ = 868_000_000


def _write_synthetic_capture(
    path: Path,
    *,
    write_auxi: bool,
    total_frames: int = 50_000,
    chunk_frames: int = 4096,
    tone_offset_hz: float = 20_000.0,
) -> None:
    settings = WaveIQWriterSettings(
        sample_rate_hz=SAMPLE_RATE_HZ,
        center_frequency_hz=CENTER_HZ,
        write_auxi=write_auxi,
    )
    writer = WaveIQWriter(path, settings)
    rng = np.random.default_rng(0)
    written = 0
    while written < total_frames:
        count = min(chunk_frames, total_frames - written)
        t = (np.arange(count, dtype=np.float64) + written) / SAMPLE_RATE_HZ
        tone = 0.3 * np.exp(1j * 2.0 * np.pi * tone_offset_hz * t)
        noise = rng.normal(scale=0.01, size=count) + 1j * rng.normal(scale=0.01, size=count)
        writer.write_frames((tone + noise).astype(np.complex64))
        written += count
    summary = writer.close()
    assert summary["frame_count"] == total_frames


def test_wave_iq_writer_round_trips_with_auxi(tmp_path: Path) -> None:
    wav = tmp_path / "capture.wav"
    _write_synthetic_capture(wav, write_auxi=True)

    info = inspect_wave_iq(wav)
    assert info.container == "RF64"
    assert info.wave_format == "WAVE"
    assert info.fmt.format_code == 1
    assert info.fmt.channels == 2
    assert info.fmt.sample_rate_hz == SAMPLE_RATE_HZ
    assert info.fmt.block_align == 4
    assert info.fmt.bits_per_sample == 16
    assert info.frame_count == 50_000
    assert abs(info.duration_seconds - 50_000 / SAMPLE_RATE_HZ) < 1e-9
    assert not info.warnings

    # Point 1 from the task: auxi is written, so center frequency resolves
    # via "auxi", exactly like a genuine SDRplay recording.
    assert info.center_frequency_source == "auxi"
    assert info.center_frequency_hz == CENTER_HZ
    assert info.auxi is not None
    assert info.auxi.layout == "sdrplay-164"
    assert info.auxi.center_frequency_hz == CENTER_HZ
    assert info.auxi.ad_frequency_hz == SAMPLE_RATE_HZ
    assert info.auxi.if_frequency_hz == 0
    assert info.auxi.bandwidth_hz == SAMPLE_RATE_HZ
    assert info.auxi.max_value == 32767
    assert info.auxi.start_time_utc is not None
    assert info.auxi.stop_time_utc is not None
    assert info.auxi.start_time_utc <= info.auxi.stop_time_utc

    assert info.ds64 is not None
    assert info.ds64.sample_count == 50_000
    assert info.ds64.data_size == 50_000 * 4

    reader = IQMemmapReader(info)
    samples = reader.read_complex(0, info.frame_count)
    assert samples.shape[0] == 50_000
    assert samples.dtype == np.complex64
    assert np.max(np.abs(samples)) <= 1.0 + 1e-6


def test_wave_iq_writer_without_auxi_falls_back_to_filename(tmp_path: Path) -> None:
    wav = tmp_path / "SDRconnect_IQ_20260815_120000_868000000HZ.wav"
    _write_synthetic_capture(wav, write_auxi=False, total_frames=10_000)

    info = inspect_wave_iq(wav)
    assert info.auxi is None
    assert info.center_frequency_source == "filename"
    assert info.center_frequency_hz == CENTER_HZ
    assert any("derived from the filename" in warning for warning in info.warnings)

    reader = IQMemmapReader(info)
    samples = reader.read_complex(0, info.frame_count)
    assert samples.shape[0] == 10_000


def test_wave_iq_writer_context_manager_closes_once(tmp_path: Path) -> None:
    wav = tmp_path / "ctx.wav"
    settings = WaveIQWriterSettings(sample_rate_hz=SAMPLE_RATE_HZ, center_frequency_hz=CENTER_HZ)
    with WaveIQWriter(wav, settings) as writer:
        writer.write_frames(np.zeros(100, dtype=np.complex64))
    with pytest.raises(ValueError):
        writer.close()

    info = inspect_wave_iq(wav)
    assert info.frame_count == 100


def test_wave_iq_writer_empty_frames_are_a_no_op(tmp_path: Path) -> None:
    wav = tmp_path / "empty_write.wav"
    settings = WaveIQWriterSettings(sample_rate_hz=SAMPLE_RATE_HZ, center_frequency_hz=CENTER_HZ)
    writer = WaveIQWriter(wav, settings)
    writer.write_frames(np.zeros(0, dtype=np.complex64))
    writer.write_frames(np.zeros(50, dtype=np.complex64))
    writer.write_frames(np.zeros(0, dtype=np.complex64))
    summary = writer.close()
    assert summary["frame_count"] == 50


def test_int16_round_trip_is_exact_for_driver_scaled_samples(tmp_path: Path) -> None:
    """SoapySDRPlay3 hands out CF32 as `adc_value / 32768.0`. Writing those
    back to int16 must return the original ADC values exactly.

    Truncating toward zero (and scaling by 32767) instead cost every
    non-zero sample 1 LSB and mapped ADC values of +/-1 to 0 -- a deadband
    at the noise floor, which is precisely where a weak-signal survey
    lives.
    """
    adc_values = np.array(
        [-32768, -100, -5, -3, -2, -1, 0, 1, 2, 3, 5, 100, 32767], dtype=np.int64
    )
    samples = (adc_values / 32768.0).astype(np.complex64)
    samples = samples + 1j * samples

    path = tmp_path / "roundtrip.wav"
    writer = WaveIQWriter(path, WaveIQWriterSettings(sample_rate_hz=200_000, center_frequency_hz=868_000_000))
    writer.write_frames(samples)
    writer.close()

    raw = np.frombuffer(path.read_bytes(), dtype="<i2")
    recovered_i = raw[-2 * adc_values.size :: 2]
    np.testing.assert_array_equal(recovered_i, adc_values.astype("<i2"))
