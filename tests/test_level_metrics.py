"""Phase 8, Stage 3: the level the solver reads as distance, measured without bias.

Two biases in the Phase 7 level were documented in docs/known-issues-v0.10.md:
`average_snr_db` is (S+N)/N, and merging windows takes its MAXIMUM. Both
raise a level, which the solver reads as "closer". `excess_snr_db` is S/N,
median across the windows that detected the channel, stored next to the old
fields rather than replacing them.
"""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pytest
from fixtures.geo_scenario import Transmitter, build_database, seed_run

from dmr_iq_surveyor.detect.core import DetectionSettings
from dmr_iq_surveyor.detect.features import detect_from_data, feature_at
from dmr_iq_surveyor.detect.merge import merge_recordings
from dmr_iq_surveyor.geo.measurements import (
    FLAG_LEVEL_DERIVED,
    LEVEL_METRIC_AVERAGE_SNR,
    LEVEL_METRIC_EXCESS_SNR,
    MeasurementSettings,
    build_run_measurements,
    censor_level_for,
    excess_from_total_snr_db,
    observation_level,
)
from dmr_iq_surveyor.survey.store import connect_survey_database

CARRIER_HZ = 162_525_000.0
NOISE_DB = -100.0


def _spectrum(excess_db: float) -> dict:
    """A flat noise floor with one 6 kHz-wide carrier `excess_db` above it."""
    frequency = np.arange(162_400_000.0, 162_650_000.0, 200.0)
    noise_linear = 10.0 ** (NOISE_DB / 10.0)
    signal = np.where(np.abs(frequency - CARRIER_HZ) <= 3_000.0, 1.0, 0.0)
    total = noise_linear * (1.0 + signal * 10.0 ** (excess_db / 10.0))
    average_db = 10.0 * np.log10(total)
    return {
        "frequency_hz": frequency,
        "average_db": average_db,
        "percentile_db": average_db + 1.0,
        "noise_db": np.full(frequency.size, NOISE_DB),
        "occupancy_pct": np.where(signal > 0, 100.0, 0.0),
        "edge_mask": np.zeros(frequency.size, dtype=bool),
        "dc_mask": np.zeros(frequency.size, dtype=bool),
    }


def test_excess_snr_is_signal_over_noise_not_total_over_noise() -> None:
    settings = DetectionSettings()
    feature = feature_at(_spectrum(0.0), CARRIER_HZ, settings)
    assert feature is not None
    # Within the integration window only part of the bins carry the
    # carrier, so both numbers are diluted by the same fill fraction; what
    # matters is that they differ exactly as (S+N)/N differs from S/N.
    total = 10.0 ** (feature["average_snr_db"] / 10.0)
    excess = 10.0 ** (feature["average_excess_snr_db"] / 10.0)
    assert excess == pytest.approx(total - 1.0, rel=1e-9)
    assert feature["average_excess_snr_db"] < feature["average_snr_db"]


def test_a_channel_with_no_excess_reports_the_floor_not_minus_infinity() -> None:
    feature = feature_at(_spectrum(-200.0), CARRIER_HZ, DetectionSettings())
    assert feature is not None
    assert math.isfinite(feature["average_excess_snr_db"])
    assert feature["average_excess_snr_db"] == -60.0


def test_merging_windows_takes_the_median_not_the_maximum() -> None:
    settings = DetectionSettings()
    recording = {"center_frequency_hz": 162_500_000.0, "sample_rate_hz": 250_000.0}
    # Four ordinary windows and one caught on a strong burst or a lucky peak.
    levels = [12.0, 14.0, 13.0, 15.0, 30.0]
    results = [
        (
            f"segment_{index}",
            detect_from_data(
                _spectrum(level),
                settings,
                recording=recording,
                source_label=f"segment_{index}",
            ),
        )
        for index, level in enumerate(levels)
    ]
    merged = [
        row for row in merge_recordings(results, settings)
        if abs(row["measured_center_hz"] - CARRIER_HZ) < 3_000.0
    ]
    assert len(merged) == 1
    candidate = merged[0]
    per_window = sorted(row["average_excess_snr_db"] for row in candidate["evidence"])
    assert candidate["excess_snr_windows"] == len(levels)
    assert candidate["excess_snr_db"] == pytest.approx(per_window[2])
    # The Phase 7 field is the max and is left exactly as it was.
    assert candidate["average_snr_db"] == pytest.approx(
        max(row["average_snr_db"] for row in candidate["evidence"])
    )
    assert candidate["average_snr_db"] - candidate["excess_snr_db"] > 10.0


@pytest.mark.parametrize(("total", "expected"), [(3.0103, 0.0), (10.4139, 10.0), (0.0, -60.0)])
def test_total_to_excess_conversion(total: float, expected: float) -> None:
    assert excess_from_total_snr_db(total) == pytest.approx(expected, abs=1e-3)


def test_censor_level_is_converted_to_the_excess_scale() -> None:
    settings = {"min_average_channel_snr_db": 4.0, "min_p95_channel_snr_db": 9.0}
    assert censor_level_for(settings, LEVEL_METRIC_AVERAGE_SNR) == 4.0
    assert censor_level_for(settings, LEVEL_METRIC_EXCESS_SNR) == pytest.approx(
        10.0 * math.log10(10.0 ** 0.4 - 1.0)
    )


def test_stored_excess_is_used_and_a_missing_one_is_derived_and_said_so() -> None:
    stored = {"snr_db": 20.0, "excess_snr_db": 15.0}
    assert observation_level(stored, LEVEL_METRIC_EXCESS_SNR) == (15.0, False)
    legacy = {"snr_db": 20.0, "excess_snr_db": None}
    level, derived = observation_level(legacy, LEVEL_METRIC_EXCESS_SNR)
    assert derived is True
    assert level == pytest.approx(excess_from_total_snr_db(20.0))
    assert observation_level(stored, LEVEL_METRIC_AVERAGE_SNR) == (20.0, False)


def test_an_old_database_gains_the_column_and_old_rows_are_flagged(tmp_path: Path) -> None:
    database = tmp_path / "db.sqlite3"
    connection = build_database(database)
    seed_run(
        connection,
        run_id="run",
        latitude=32.052,
        longitude=34.802,
        transmitters=[Transmitter(867_762_500.0, 32.050, 34.800, reference_level_db=30.0)],
    )
    connection.close()
    connection = connect_survey_database(database)
    columns = {row[1] for row in connection.execute("PRAGMA table_info(rf_observations)")}
    assert "excess_snr_db" in columns
    connection.close()

    from dmr_iq_surveyor.geo.store import connect_geo_database

    connection = connect_geo_database(database)
    rows = build_run_measurements(
        connection, "run", MeasurementSettings(level_metric=LEVEL_METRIC_EXCESS_SNR)
    )
    heard = next(row for row in rows if row["detected"])
    assert FLAG_LEVEL_DERIVED in heard["quality_flags"]
    phase7 = build_run_measurements(connection, "run")
    assert all(FLAG_LEVEL_DERIVED not in row["quality_flags"] for row in phase7)
    connection.close()


def test_a_survey_run_stores_excess_snr_beside_the_phase7_fields(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    from fixtures.synthetic import SyntheticTone, write_synthetic_iq_wav

    from dmr_iq_surveyor.survey.pipeline import run_survey
    from dmr_iq_surveyor.survey.profiles import BandProfile, SiteProfile

    wav = tmp_path / "recording.wav"
    write_synthetic_iq_wav(
        wav,
        sample_rate_hz=200_000,
        center_frequency_hz=868_000_000,
        duration_seconds=6.0,
        tones=[SyntheticTone(offset_hz=50_000.0, amplitude=0.35)],
        capture_start_utc=datetime(2026, 8, 1, tzinfo=UTC),
    )
    band = BandProfile(
        name="test_band",
        label="test",
        start_frequency_hz=867_800_000.0,
        stop_frequency_hz=868_200_000.0,
        raster_spacings_hz=[12500.0, 6250.0],
        detection_overrides={
            "scan_step_hz": 6250.0,
            "integration_width_hz": 12500.0,
            "min_p95_channel_snr_db": 9.0,
            "min_average_channel_snr_db": 4.0,
            "min_equivalent_width_hz": 1500.0,
            "min_width_90_hz": 1000.0,
            "max_width_90_hz": 13000.0,
            "merge_tolerance_hz": 4000.0,
            "passband_warning_low_hz": 866_000_000.0,
            "passband_warning_high_hz": 870_000_000.0,
        },
        segment_seconds=1.0,
        segment_stride_seconds=1.0,
        max_segments=10,
    )
    database = tmp_path / "db.sqlite3"
    run_survey(
        wav,
        tmp_path / "out",
        band=band,
        site=SiteProfile(site_id="home", label="Home"),
        run_id="r1",
        database_path=database,
        spectrum_fft_size=4096,
    )
    connection = connect_survey_database(database)
    rows = connection.execute("SELECT snr_db, excess_snr_db FROM rf_observations").fetchall()
    connection.close()
    assert rows
    for snr_db, excess_snr_db in rows:
        assert excess_snr_db is not None
        assert excess_snr_db < snr_db
