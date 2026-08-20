from __future__ import annotations

from pathlib import Path

import pytest

from dmr_iq_surveyor.capture.preflight import (
    BYTES_PER_FRAME,
    capture_size_bytes,
    max_sustainable_sample_rate,
    measure_write_throughput,
    required_bytes_per_second,
    run_preflight,
)
from dmr_iq_surveyor.survey.profiles import BandProfile


def _band(start_hz: float = 866_000_000.0, stop_hz: float = 870_000_000.0) -> BandProfile:
    return BandProfile(
        name="test_band",
        label="test",
        start_frequency_hz=start_hz,
        stop_frequency_hz=stop_hz,
        raster_spacings_hz=[12500.0],
        detection_overrides={},
    )


def test_required_throughput_matches_16bit_iq_math() -> None:
    # 10 MS/s of interleaved signed 16-bit I/Q is 40 MB/s -- the number the
    # whole field storage decision hangs on.
    assert required_bytes_per_second(10_000_000.0) == 40_000_000.0
    assert required_bytes_per_second(2_000_000.0) == 8_000_000.0
    assert BYTES_PER_FRAME == 4


def test_capture_size_matches_observed_file_sizes() -> None:
    # A real 5.0039 s capture at 10 MS/s produced 200,156,544 bytes of IQ.
    assert capture_size_bytes(10_000_000.0, 5.003914) == pytest.approx(200_156_560, rel=1e-6)


def test_max_sustainable_sample_rate_applies_a_safety_margin() -> None:
    # 40 MB/s of measured throughput must NOT authorise a 10 MS/s capture,
    # which needs exactly 40 MB/s with nothing left over.
    assert max_sustainable_sample_rate(40_000_000.0) < 10_000_000.0


def test_measure_write_throughput_returns_a_positive_rate(tmp_path: Path) -> None:
    rate = measure_write_throughput(tmp_path, megabytes=2)
    assert rate > 0
    # The probe file must not be left behind to fill the operator's disk.
    assert not list(tmp_path.glob(".dmr_surveyor_write_probe*"))


def test_preflight_fails_when_band_is_not_covered_at_all(tmp_path: Path) -> None:
    result = run_preflight(
        tmp_path,
        band=_band(),
        center_frequency_hz=150_000_000.0,  # nowhere near the 800 MHz band
        sample_rate_hz=2_000_000.0,
        duration_seconds=10.0,
        skip_throughput=True,
    )
    coverage = next(c for c in result["checks"] if c["name"] == "Band coverage")
    assert coverage["status"] == "fail"
    assert result["verdict"] == "fail"


def test_preflight_warns_on_partial_band_coverage(tmp_path: Path) -> None:
    """2 MS/s cannot cover a 4 MHz band -- the operator must be told before
    the capture, not left to discover it in the report afterwards."""
    result = run_preflight(
        tmp_path,
        band=_band(),
        center_frequency_hz=868_000_000.0,
        sample_rate_hz=2_000_000.0,
        duration_seconds=10.0,
        skip_throughput=True,
    )
    coverage = next(c for c in result["checks"] if c["name"] == "Band coverage")
    assert coverage["status"] == "warn"
    assert "50%" in coverage["detail"]


def test_preflight_passes_band_coverage_when_nyquist_spans_the_band(tmp_path: Path) -> None:
    result = run_preflight(
        tmp_path,
        band=_band(),
        center_frequency_hz=868_000_000.0,
        sample_rate_hz=10_000_000.0,
        duration_seconds=10.0,
        skip_throughput=True,
    )
    coverage = next(c for c in result["checks"] if c["name"] == "Band coverage")
    assert coverage["status"] == "pass"


def test_preflight_fails_when_disk_cannot_hold_the_capture(tmp_path: Path) -> None:
    """A 10 MS/s capture for 8 hours is far larger than any test filesystem;
    preflight must refuse rather than fill the disk mid-capture."""
    result = run_preflight(
        tmp_path,
        band=_band(),
        center_frequency_hz=868_000_000.0,
        sample_rate_hz=10_000_000.0,
        duration_seconds=8 * 3600.0,
        skip_throughput=True,
    )
    space = next(c for c in result["checks"] if c["name"] == "Free space")
    assert space["status"] == "fail"
    assert result["verdict"] == "fail"


def test_preflight_reports_gps_not_configured_as_a_warning_not_a_failure(tmp_path: Path) -> None:
    result = run_preflight(
        tmp_path,
        band=_band(),
        center_frequency_hz=868_000_000.0,
        sample_rate_hz=10_000_000.0,
        duration_seconds=10.0,
        skip_throughput=True,
    )
    gps = next(c for c in result["checks"] if c["name"] == "GPS")
    assert gps["status"] == "warn"


def test_preflight_gps_failure_never_escalates_to_no_go(tmp_path: Path) -> None:
    """An unreachable phone must not block a capture -- GPS is supplementary."""
    result = run_preflight(
        tmp_path,
        band=_band(),
        center_frequency_hz=868_000_000.0,
        sample_rate_hz=10_000_000.0,
        duration_seconds=10.0,
        skip_throughput=True,
        gps_url="http://127.0.0.1:1/location",
        gps_timeout_seconds=1.0,
    )
    gps = next(c for c in result["checks"] if c["name"] == "GPS")
    assert gps["status"] == "warn"
    assert "captures still work" in gps["detail"]
