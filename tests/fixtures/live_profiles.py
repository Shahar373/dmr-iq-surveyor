"""Band and site profiles for live-survey tests, plus the tone they expect.

The live pipeline resolves profiles from files exactly as the field app does,
so a test that exercises it needs real ones. They are written per test rather
than checked in under `config/`, because a fixture band that shipped with the
project would be one more profile an operator could pick by mistake.
"""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np

# Only the raster and the SNR gates. The shape gates a real 800 MHz profile
# carries (equivalent width, fill ratio, peak concentration) describe a
# modulated P25 carrier; a test tone is a few hertz wide and would fail every
# one of them, which would test the fixture rather than the live pipeline.
BAND_YAML = """
name: live_test
label: "narrow test band"
start_frequency_hz: {low:.0f}
stop_frequency_hz: {high:.0f}
raster_spacings_hz:
  - 12500
  - 6250
detection:
  scan_step_hz: 6250
  integration_width_hz: 12500
  min_p95_channel_snr_db: 9.0
  min_average_channel_snr_db: 4.0
  merge_tolerance_hz: 4000
segment_seconds: 1.0
segment_stride_seconds: 1.0
max_segments: 40
usable_passband_rolloff_db: 3.0
comparison:
  frequency_tolerance_hz: 6250
  snr_delta_db: 3.0
  occupancy_delta_pct: 10.0
  persistence_delta: 0.25
  analyzed_seconds_ratio_limit: 4.0
"""

SITE_YAML = """
site_id: mobile
label: "Mobile receiver"
latitude: null
longitude: null
antenna: null
receiver: "SDRplay RSP1A"
gain_mode: manual
gain: 26.0
lna_state: 8
notes: ""
"""


def write_profiles(
    directory: Path, *, center_hz: float, half_width_hz: float = 90_000.0
) -> tuple[Path, Path]:
    """(band path, site path) for a band centred on the receiver's tuning."""
    directory.mkdir(parents=True, exist_ok=True)
    band = directory / "live_test.yaml"
    band.write_text(
        BAND_YAML.format(low=center_hz - half_width_hz, high=center_hz + half_width_hz),
        encoding="utf-8",
    )
    site = directory / "mobile.yaml"
    site.write_text(SITE_YAML, encoding="utf-8")
    return band, site


def tone_chunk(
    frames: int,
    *,
    phase: int,
    offset_hz: float,
    sample_rate_hz: float,
    level_db: float,
    rng: np.random.Generator,
) -> np.ndarray:
    """One chunk of a carrier at `offset_hz` from centre, at `level_db`.

    The level is a free scale, not dBm: what the geolocation model reads is
    how levels DIFFER between places, so only the exponent and the spread
    matter. 45 dB at 1 km with an exponent of 3 puts a 2 km measurement near
    17 dB SNR and a 400 m one near 38 dB -- a real gradient rather than
    "detected everywhere".
    """
    amplitude = 10.0 ** (level_db / 20.0) * 1e-3
    index = np.arange(phase, phase + frames, dtype=np.float64)
    tone = amplitude * np.exp(2j * np.pi * offset_hz * index / sample_rate_hz)
    noise = rng.normal(scale=0.02, size=frames) + 1j * rng.normal(scale=0.02, size=frames)
    return (tone + noise).astype(np.complex64)


def level_at(
    distance_m: float, *, reference_level_db: float = 45.0, exponent: float = 3.0
) -> float:
    return reference_level_db - 10.0 * exponent * math.log10(max(distance_m, 50.0) / 1000.0)


__all__ = ["BAND_YAML", "SITE_YAML", "level_at", "tone_chunk", "write_profiles"]
