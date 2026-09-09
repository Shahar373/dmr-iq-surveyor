"""Channels that missed the detection gate by a little.

Not detections, never evidence -- a hint to a moving receiver that a longer
measurement where it stands would probably settle them. Tested at the level
of the spectrum arrays the detector actually reads, so the assertions are
about the gate arithmetic and not about a synthesised radio.
"""

from __future__ import annotations

import numpy as np
from fixtures.live_profiles import write_profiles

from dmr_iq_surveyor.detect.core import DetectionSettings
from dmr_iq_surveyor.detect.features import NEAR_THRESHOLD_MARGIN_DB, detect_from_data
from dmr_iq_surveyor.spectrum.core import SpectrumSettings
from dmr_iq_surveyor.survey.discovery import SegmentSpectrum, observations_from_segments
from dmr_iq_surveyor.survey.profiles import load_band_profile

RATE = 5_000_000.0
CENTER = 867_406_250.0
FFT = 16_384
FLOOR_DB = -90.0
GATE_DB = 9.0


def _spectrum(channels: dict[float, float]) -> dict[str, np.ndarray]:
    """A flat noise floor with raised 12.5 kHz blocks at the given centres.

    `channels` maps centre frequency to how many dB the block's 95th
    percentile sits above the floor. The average is set 3 dB under the
    percentile, as a modulated carrier's is, so the average gate (4 dB) is
    cleared whenever the percentile gate is close.
    """
    frequency = CENTER + np.fft.fftshift(np.fft.fftfreq(FFT, 1 / RATE))
    noise = np.full(FFT, FLOOR_DB)
    average = noise.copy()
    percentile = noise + 2.0  # a percentile of pure noise sits a little above its mean
    for centre, excess in channels.items():
        block = np.abs(frequency - centre) <= 6_250.0
        percentile[block] = FLOOR_DB + excess
        average[block] = FLOOR_DB + excess - 3.0
    return {
        "frequency_hz": frequency,
        "average_db": average,
        "percentile_db": percentile,
        "noise_db": noise,
        "occupancy_pct": np.where(percentile > FLOOR_DB + 5.0, 95.0, 5.0),
        "edge_mask": np.zeros(FFT, dtype=bool),
        "dc_mask": np.zeros(FFT, dtype=bool),
    }


def _segment(channels: dict[float, float]) -> SegmentSpectrum:
    data = _spectrum(channels)
    return SegmentSpectrum(
        frequency_hz=data["frequency_hz"],
        average_db=data["average_db"].astype(np.float32),
        percentile_db=data["percentile_db"].astype(np.float32),
        noise_db=data["noise_db"].astype(np.float32),
        occupancy_pct=data["occupancy_pct"].astype(np.float32),
        edge_mask=data["edge_mask"],
        dc_mask=data["dc_mask"],
        fft_count=24,
    )


STRONG = 867_762_500.0   # site 30's control channel in the fixtures
NEAR = 866_712_500.0     # site 33's
FAINT = 867_912_500.0    # a third raster step, well under everything

SETTINGS = DetectionSettings(
    scan_step_hz=6_250.0, integration_width_hz=12_500.0,
    min_p95_channel_snr_db=GATE_DB, min_average_channel_snr_db=4.0, merge_tolerance_hz=4_000.0,
)


def test_a_near_miss_is_reported_apart_from_a_detection() -> None:
    data = _spectrum({STRONG: GATE_DB + 6.0, NEAR: GATE_DB - 1.5, FAINT: GATE_DB - 6.0})
    result = detect_from_data(
        data, SETTINGS, recording={}, source_label="t",
        scan_low_hz=866_000_000.0, scan_high_hz=868_800_000.0,
    )
    assert result["strong_window_count"] >= 1, "the strong channel must clear the gate"
    near = {round(f["frequency_hz_assuming_iq"]) for f in result["near_threshold"]}
    assert round(NEAR) in near, "1.5 dB under the gate is a near miss"
    assert round(STRONG) not in near, "a detection is not also a near miss"
    assert round(FAINT) not in near, (
        f"6 dB under is outside the {NEAR_THRESHOLD_MARGIN_DB:g} dB margin: not something a "
        "longer measurement would plausibly close"
    )


def test_near_misses_need_a_majority_of_windows_and_must_not_be_detected(tmp_path) -> None:
    """One noisy window is not a hint. And a channel that made it to an
    observation is a detection, whatever it looked like in some windows."""
    band, _site = write_profiles(tmp_path, center_hz=CENTER, half_width_hz=1_400_000.0)
    profile = load_band_profile(band)
    spectrum_settings = SpectrumSettings(fft_size=FFT, overlap_ratio=0.5)

    steady_near = {NEAR: GATE_DB - 1.5}
    segments = (
        [_segment({STRONG: GATE_DB + 6.0, **steady_near}) for _ in range(8)]
        + [_segment({STRONG: GATE_DB + 6.0, NEAR: GATE_DB - 6.0}) for _ in range(2)]
    )
    # A channel near in only three of ten windows: below the majority.
    for index in range(3):
        segments[index] = _segment({STRONG: GATE_DB + 6.0, **steady_near, FAINT: GATE_DB - 1.0})

    detection = observations_from_segments(
        segments, band_profile=profile, center_frequency_hz=CENTER,
        sample_rate_hz=RATE, spectrum_settings=spectrum_settings,
    )
    detected = {round(o.measured_center_hz / 6_250.0) * 6_250.0 for o in detection["observations"]}
    near = {entry["frequency_hz"]: entry for entry in detection["near_threshold"]}

    assert STRONG in detected, "the strong channel is an observation"
    assert STRONG not in near
    assert NEAR in near, "near in eight of ten windows is a hint"
    assert near[NEAR]["segments_near"] == 8
    assert near[NEAR]["segments_analyzed"] == 10
    assert GATE_DB - 3.0 <= near[NEAR]["p95_snr_db"] < GATE_DB
    assert FAINT not in near, "near in three of ten windows is noise, not a hint"
    assert NEAR not in detected, "a hint must never be written as evidence"
