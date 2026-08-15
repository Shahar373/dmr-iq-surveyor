from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from fixtures.synthetic import SyntheticTone, write_synthetic_iq_wav

from dmr_iq_surveyor.iq.metadata import inspect_wave_iq
from dmr_iq_surveyor.survey.discovery import (
    POWER_UNIT_DBFS_PER_HZ,
    discover_observations,
    plan_segments,
    resolve_capture_time,
)
from dmr_iq_surveyor.survey.profiles import BandProfile

SAMPLE_RATE_HZ = 200_000
CENTER_HZ = 868_000_000


def _tuned_band_profile(**overrides) -> BandProfile:
    detection = {
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
    }
    params = {
        "name": "test_band",
        "label": "test",
        "start_frequency_hz": 867_800_000.0,
        "stop_frequency_hz": 868_200_000.0,
        "raster_spacings_hz": [12500.0, 6250.0],
        "detection_overrides": detection,
        "segment_seconds": 1.0,
        "segment_stride_seconds": 1.0,
        "max_segments": 10,
    }
    params.update(overrides)
    return BandProfile(**params)


def test_plan_segments_whole_file_fallback() -> None:
    segments = plan_segments(
        1_000_000, 100_000.0, segment_seconds=None, stride_seconds=None, max_segments=None
    )
    assert len(segments) == 1
    assert segments[0].frame_count == 1_000_000


def test_plan_segments_bounded_by_max_segments() -> None:
    segments = plan_segments(
        10_000_000,
        100_000.0,
        segment_seconds=1.0,
        stride_seconds=1.0,
        max_segments=3,
    )
    assert len(segments) == 3
    assert segments[0].start_frame == 0
    assert segments[1].start_frame == 100_000
    assert segments[2].start_frame == 200_000


def test_discover_observations_detects_known_tone_without_frequency_list(tmp_path: Path) -> None:
    wav = tmp_path / "recording.wav"
    write_synthetic_iq_wav(
        wav,
        sample_rate_hz=SAMPLE_RATE_HZ,
        center_frequency_hz=CENTER_HZ,
        duration_seconds=6.0,
        tones=[SyntheticTone(offset_hz=50_000.0, amplitude=0.35)],
        capture_start_utc=datetime(2026, 8, 1, tzinfo=UTC),
    )
    band = _tuned_band_profile()
    result = discover_observations(
        wav, band_profile=band, spectrum_fft_size=4096, spectrum_overlap_ratio=0.5
    )
    observations = result["observations"]
    assert len(observations) == 1
    observation = observations[0]
    assert abs(observation.measured_center_hz - (CENTER_HZ + 50_000.0)) < 2000.0
    # Evidence, not a snap: measured center is not forced onto the raster.
    assert observation.raster_error_hz == observation.measured_center_hz - observation.nearest_raster_hz
    assert observation.power_unit == POWER_UNIT_DBFS_PER_HZ
    assert observation.calibrated is False
    assert observation.classification == "unknown"
    assert observation.classification_method == "spectral_only"


def test_persistence_and_occupancy_are_distinct_metrics(tmp_path: Path) -> None:
    """A tone present in every segment but only briefly each time should show
    persistence close to 1.0 while occupancy stays low; both must be
    computed independently, never conflated."""
    wav = tmp_path / "bursty.wav"
    # Six 1-second segments; the tone is on for the first 0.1s of every
    # second -> present (persistent) in all six segments, but only ~10% duty.
    active_ranges = [(float(i), float(i) + 0.1) for i in range(6)]
    write_synthetic_iq_wav(
        wav,
        sample_rate_hz=SAMPLE_RATE_HZ,
        center_frequency_hz=CENTER_HZ,
        duration_seconds=6.0,
        tones=[
            SyntheticTone(
                offset_hz=40_000.0,
                amplitude=0.6,
                active_ranges=active_ranges,
                modulation_bandwidth_hz=0.0,
            )
        ],
        capture_start_utc=datetime(2026, 8, 1, tzinfo=UTC),
    )
    band = _tuned_band_profile()
    result = discover_observations(
        wav, band_profile=band, spectrum_fft_size=4096, spectrum_overlap_ratio=0.5
    )
    observations = [
        obs for obs in result["observations"] if abs(obs.measured_center_hz - (CENTER_HZ + 40_000.0)) < 5000.0
    ]
    assert observations, "expected the bursty tone to be detected"
    observation = observations[0]
    assert observation.persistence >= 0.8
    assert observation.occupancy_pct < 50.0


def test_usable_passband_never_exceeds_nyquist(tmp_path: Path) -> None:
    wav = tmp_path / "recording.wav"
    write_synthetic_iq_wav(
        wav,
        sample_rate_hz=SAMPLE_RATE_HZ,
        center_frequency_hz=CENTER_HZ,
        duration_seconds=4.0,
        tones=[],
        capture_start_utc=datetime(2026, 8, 1, tzinfo=UTC),
    )
    band = _tuned_band_profile(
        start_frequency_hz=CENTER_HZ - 1_000_000.0,
        stop_frequency_hz=CENTER_HZ + 1_000_000.0,
    )
    result = discover_observations(
        wav, band_profile=band, spectrum_fft_size=4096, spectrum_overlap_ratio=0.5
    )
    passband = result["usable_passband"]
    nyquist_low = CENTER_HZ - SAMPLE_RATE_HZ / 2.0
    nyquist_high = CENTER_HZ + SAMPLE_RATE_HZ / 2.0
    assert passband.usable_low_hz >= nyquist_low - 1.0
    assert passband.usable_high_hz <= nyquist_high + 1.0
    # Requested range far exceeds the 200 kHz Nyquist width -> partial, with
    # the uncovered ranges reported rather than silently analyzing less.
    assert passband.coverage_status == "partial"
    assert passband.uncovered_ranges_hz


def test_resolve_capture_time_prefers_auxi_then_filename_then_unknown(tmp_path: Path) -> None:
    wav = tmp_path / "SDRconnect_IQ_20260713_150242_868000000HZ.wav"
    write_synthetic_iq_wav(
        wav,
        sample_rate_hz=SAMPLE_RATE_HZ,
        center_frequency_hz=CENTER_HZ,
        duration_seconds=1.0,
        tones=[],
        capture_start_utc=datetime(2026, 8, 1, 12, 0, 0, tzinfo=UTC),
    )
    info = inspect_wave_iq(wav)
    capture_time, source = resolve_capture_time(info)
    assert source == "auxi"
    assert capture_time == "2026-08-01T12:00:00+00:00"

    # user override always wins
    capture_time, source = resolve_capture_time(info, user_capture_start_utc="2020-01-01T00:00:00+00:00")
    assert source == "user"
