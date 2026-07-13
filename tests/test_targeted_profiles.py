from __future__ import annotations

import sqlite3
from pathlib import Path

import numpy as np
import pytest

from dmr_iq_surveyor.decode.core import (
    normalize_pcm16,
    process_complex_chunks,
)
from dmr_iq_surveyor.decode.profiles import extraction_profile
from dmr_iq_surveyor.inventory.store import connect_database


@pytest.mark.parametrize(
    ("profile", "rate", "intermediate_rate"),
    [
        ("250k", 250_000, 50_000),
        ("500k", 500_000, 100_000),
        ("10m", 10_000_000, 100_000),
    ],
)
def test_profiles_validate_expected_rates(
    profile: str,
    rate: int,
    intermediate_rate: int,
) -> None:
    settings = extraction_profile(profile, rate)
    assert settings.validate(rate) == intermediate_rate
    assert extraction_profile("auto", rate).to_dict() == settings.to_dict()


def test_profile_rate_mismatch_fails_before_processing() -> None:
    with pytest.raises(ValueError, match="requires 500,000 S/s"):
        extraction_profile("500k", 250_000)
    with pytest.raises(ValueError, match="No automatic extraction profile"):
        extraction_profile("auto", 1_000_000)


@pytest.mark.parametrize(("profile", "rate"), [("250k", 250_000), ("500k", 500_000)])
def test_targeted_profiles_recover_fm_and_remain_peak_safe(
    profile: str,
    rate: int,
) -> None:
    duration = 0.20
    count = int(rate * duration)
    time = np.arange(count) / rate
    audio_hz = 1_000.0
    deviation_hz = 1_800.0
    instantaneous = deviation_hz * np.sin(2 * np.pi * audio_hz * time)
    phase = 2 * np.pi * np.cumsum(instantaneous) / rate
    iq = np.exp(1j * phase).astype(np.complex64)
    settings = extraction_profile(profile, rate, chunk_frames=count // 3)
    chunks = [
        iq[start : start + settings.chunk_frames]
        for start in range(0, len(iq), settings.chunk_frames)
    ]
    output, metrics, _preview = process_complex_chunks(
        chunks,
        input_sample_rate_hz=rate,
        frequency_offset_hz=0.0,
        settings=settings,
    )
    expected_duration = duration - 2 * settings.trim_seconds
    assert abs(len(output) / 48_000 - expected_duration) < 0.003
    spectrum = np.fft.rfft(output * np.hanning(len(output)))
    frequencies = np.fft.rfftfreq(len(output), 1 / 48_000)
    peak = frequencies[np.argmax(np.abs(spectrum[1:])) + 1]
    assert abs(peak - audio_hz) < 25.0
    pcm, normalization = normalize_pcm16(
        output,
        settings.normalization_percentile,
        settings.output_peak_fraction,
    )
    assert metrics["intermediate_rate_hz"] in {50_000, 100_000}
    assert normalization["clipped_samples"] == 0
    assert np.max(np.abs(pcm.astype(np.int32))) <= round(32767 * 0.9)


def test_existing_inventory_database_is_migrated_for_metadata(
    tmp_path: Path,
) -> None:
    database = tmp_path / "inventory.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            CREATE TABLE attempts (
                attempt_key TEXT PRIMARY KEY,
                output_dir TEXT NOT NULL
            )
            """
        )
    connection = connect_database(database)
    try:
        columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(attempts)")
        }
    finally:
        connection.close()
    assert "capture_metadata_json" in columns
