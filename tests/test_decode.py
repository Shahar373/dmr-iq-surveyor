from __future__ import annotations

import wave
from pathlib import Path

import numpy as np
from scipy.signal import freqz

from dmr_iq_surveyor.decode.core import (
    ExtractionSettings,
    StreamingFMDiscriminator,
    StreamingMixer,
    design_filters,
    normalize_pcm16,
    process_complex_chunks,
)
from dmr_iq_surveyor.decode.dsd import (
    DecoderSettings,
    parse_dsd_fme_log,
    probe_decoder,
    run_decoder_attempt,
)
from dmr_iq_surveyor.decode.extract import write_pcm16_wav


def test_streaming_mixer_matches_single_block() -> None:
    rate = 1_000_000
    offset = 123_456.0
    samples = np.ones(25_000, dtype=np.complex64)
    whole = StreamingMixer(offset, rate).process(samples)
    mixer = StreamingMixer(offset, rate)
    chunked = np.concatenate(
        [
            mixer.process(samples[:7777]),
            mixer.process(samples[7777:]),
        ]
    )
    assert np.max(np.abs(whole - chunked)) < 2e-6


def test_streaming_discriminator_keeps_boundary_pair() -> None:
    phase_step = 0.07
    values = np.exp(
        1j * phase_step * np.arange(1000)
    ).astype(np.complex64)
    whole = StreamingFMDiscriminator().process(values)
    decoder = StreamingFMDiscriminator()
    chunked = np.concatenate(
        [
            decoder.process(values[:333]),
            decoder.process(values[333:]),
        ]
    )
    assert len(whole) == len(chunked)
    assert np.max(np.abs(whole - chunked)) < 1e-6


def test_channel_filter_rejects_adjacent_channel() -> None:
    settings = ExtractionSettings()
    taps = design_filters(10_000_000, settings)["channel"]
    frequency, response = freqz(
        taps,
        worN=65_536,
        fs=100_000,
    )
    at_5k = np.abs(
        response[np.argmin(np.abs(frequency - 5_000))]
    )
    at_12k5 = np.abs(
        response[np.argmin(np.abs(frequency - 12_500))]
    )
    attenuation_db = 20 * np.log10(
        max(at_12k5, 1e-12) / max(at_5k, 1e-12)
    )
    assert attenuation_db < -45.0


def test_chunked_pipeline_recovers_fm_audio_and_duration() -> None:
    rate = 10_000_000
    duration = 0.12
    count = int(rate * duration)
    time = np.arange(count) / rate
    audio_hz = 1000.0
    deviation_hz = 1800.0
    carrier_offset_hz = 123_000.0
    instantaneous = (
        carrier_offset_hz
        + deviation_hz * np.sin(2 * np.pi * audio_hz * time)
    )
    phase = 2 * np.pi * np.cumsum(instantaneous) / rate
    iq = np.exp(1j * phase).astype(np.complex64)
    settings = ExtractionSettings(
        chunk_frames=333_333,
        trim_seconds=0.003,
    )
    chunks = [
        iq[start : start + settings.chunk_frames]
        for start in range(0, len(iq), settings.chunk_frames)
    ]
    output, metrics, _preview = process_complex_chunks(
        chunks,
        input_sample_rate_hz=rate,
        frequency_offset_hz=carrier_offset_hz,
        settings=settings,
    )
    expected_duration = duration - 2 * settings.trim_seconds
    assert (
        abs(
            len(output) / settings.output_rate_hz
            - expected_duration
        )
        < 0.002
    )
    spectrum = np.fft.rfft(
        output * np.hanning(len(output))
    )
    frequency = np.fft.rfftfreq(
        len(output),
        1 / settings.output_rate_hz,
    )
    peak = frequency[np.argmax(np.abs(spectrum[1:])) + 1]
    assert abs(peak - audio_hz) < 20.0
    assert metrics["intermediate_rate_hz"] == 100_000


def test_normalization_is_robust_and_clipped_counted() -> None:
    samples = np.concatenate(
        [
            np.linspace(-1, 1, 10_000),
            np.array([20.0]),
        ]
    ).astype(np.float32)
    pcm, metrics = normalize_pcm16(samples, 99.5, 0.9)
    assert pcm.dtype == np.int16
    assert metrics["clipped_samples"] >= 1
    assert metrics["peak_pcm"] == 32767


def test_pcm_wav_is_mono_48k_16bit(tmp_path: Path) -> None:
    samples = np.arange(-1000, 1000, dtype=np.int16)
    path = tmp_path / "discriminator.wav"
    write_pcm16_wav(path, samples, 48_000)
    with wave.open(str(path), "rb") as handle:
        assert handle.getnchannels() == 1
        assert handle.getsampwidth() == 2
        assert handle.getframerate() == 48_000
        assert handle.getnframes() == len(samples)


def test_parser_extracts_explicit_dmr_evidence() -> None:
    log = """
00:43:00 Sync: +DMR  [slot1]  slot2  | Color Code=02 | CSBK
Talkgroup Voice Channel Grant - Target: 16777215 - Source: 64250
00:43:01 Sync: +DMR   slot1  [slot2] | Color Code=02 | Voice
TG: 1234 SRC: 5678
"""
    result = parse_dsd_fme_log("", log)
    assert result["dmr_sync_count"] == 2
    assert result["explicit_dmr_sync"] is True
    assert result["color_codes"] == [2]
    assert 1234 in result["talkgroup_ids"]
    assert 16777215 in result["talkgroup_ids"]
    assert 5678 in result["radio_ids"]
    assert 64250 in result["radio_ids"]
    assert result["slot1_sync_count"] == 2
    assert result["slot2_sync_count"] == 2


def test_missing_decoder_is_documented(tmp_path: Path) -> None:
    settings = DecoderSettings(
        binary="definitely-not-a-real-dsd-fme-binary",
        inversions=["normal"],
    )
    probe = probe_decoder(settings.binary)
    result = run_decoder_attempt(
        tmp_path / "input.wav",
        tmp_path / "decoder",
        settings=settings,
        inversion="normal",
        probe=probe,
    )
    assert result["status"] == "decoder_unavailable"
    assert (
        tmp_path
        / "decoder"
        / "decoder_report_normal.json"
    ).is_file()
    assert (
        tmp_path
        / "decoder"
        / "dsd_fme_normal_stderr.log"
    ).is_file()
