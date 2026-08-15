"""Optional integration validation against a real local 800 MHz capture.

Skipped entirely unless `DMR_SURVEYOR_TEST_RECORDING` points at an existing
file. The real recording
(`p25_866_870_20260808_214241_867881250HZ.wav`) is never checked into the
repository and this test must not require it to pass -- the rest of the
suite is exclusively synthetic. Run manually with:

    DMR_SURVEYOR_TEST_RECORDING=/path/to/p25_866_870_20260808_214241_867881250HZ.wav \
        pytest tests/test_survey_real_recording.py -v
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from dmr_iq_surveyor.survey.discovery import discover_observations
from dmr_iq_surveyor.survey.profiles import resolve_band_profile

REPO_ROOT = Path(__file__).resolve().parents[1]
_RECORDING_PATH = os.environ.get("DMR_SURVEYOR_TEST_RECORDING")

pytestmark = pytest.mark.skipif(
    not _RECORDING_PATH or not Path(_RECORDING_PATH).is_file(),
    reason="DMR_SURVEYOR_TEST_RECORDING is not set to an existing local recording",
)


def test_real_800mhz_capture_survey_completes_and_reports_coverage() -> None:
    band = resolve_band_profile("central_800", base_dir=REPO_ROOT)
    result = discover_observations(_RECORDING_PATH, band_profile=band)
    passband = result["usable_passband"]
    assert passband.coverage_status in {"complete", "partial"}
    assert result["segments_analyzed"] > 0
    # Documented as an existing candidate near 867.262500 MHz; soft check
    # within a generous tolerance since exact center depends on capture gain.
    near_known_candidate = any(
        abs(obs.measured_center_hz - 867_262_500.0) < 50_000.0 for obs in result["observations"]
    )
    assert near_known_candidate, (
        f"expected a candidate near 867.2625 MHz, got "
        f"{[obs.measured_center_hz for obs in result['observations']]}"
    )
