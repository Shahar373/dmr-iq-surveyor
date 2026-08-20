"""Pre-capture field checks: answer "will this capture actually work?"
before committing to it, rather than discovering the answer 90 seconds in.

Everything here is read-only with respect to the SDR (it probes, it never
streams) and writes only one temporary file, to the same filesystem the
capture would use, to measure real sustained write throughput. That last
check is the one that matters most in practice: a 10 MS/s capture needs
40 MB/s of sustained writes, and storage that cannot keep up truncates or
drops samples rather than failing cleanly.
"""

from __future__ import annotations

import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dmr_iq_surveyor.capture.device import probe_soapysdr
from dmr_iq_surveyor.capture.gps import GpsFixError, fetch_gps_fix
from dmr_iq_surveyor.survey.profiles import BandProfile

BYTES_PER_FRAME = 4  # interleaved signed 16-bit I and Q

# Sustained write throughput must exceed the sample rate's demand by this
# factor. Storage benchmarks are optimistic relative to a long real capture
# (thermal throttling, filesystem metadata, competing I/O), and there is no
# recovering a dropped sample after the fact.
_THROUGHPUT_SAFETY_FACTOR = 1.25
_DEFAULT_PROBE_MEGABYTES = 128

PASS = "pass"
WARN = "warn"
FAIL = "fail"

# SoapySDRPlay3 picks the analog IF filter from the sample rate, and the
# available filters are far from a continuum. Transcribed from
# `SoapySDRPlay3/Settings.cpp::getBwEnumForRate`. The consequence that
# matters in the field: every rate from 1.536 up to (not including) 5 MS/s
# gets the same 1.536 MHz filter, so paying 4 MS/s of storage bandwidth buys
# no more usable spectrum than 2 MS/s does. The next real step up is 5 MS/s.
_IF_BANDWIDTH_STEPS_HZ: tuple[tuple[float, float], ...] = (
    (300_000.0, 200_000.0),
    (600_000.0, 300_000.0),
    (1_536_000.0, 600_000.0),
    (5_000_000.0, 1_536_000.0),
    (6_000_000.0, 5_000_000.0),
    (7_000_000.0, 6_000_000.0),
    (8_000_000.0, 7_000_000.0),
)
_MAX_IF_BANDWIDTH_HZ = 8_000_000.0


def sdrplay_if_bandwidth_hz(sample_rate_hz: float) -> float:
    """Analog IF filter width SoapySDRPlay3 will select for `sample_rate_hz`."""
    for threshold, bandwidth in _IF_BANDWIDTH_STEPS_HZ:
        if sample_rate_hz < threshold:
            return bandwidth
    return _MAX_IF_BANDWIDTH_HZ


def usable_span_hz(sample_rate_hz: float) -> float:
    """Spectrum actually usable at this rate: the narrower of the Nyquist
    width and the analog filter the receiver will apply."""
    return min(sample_rate_hz, sdrplay_if_bandwidth_hz(sample_rate_hz))


@dataclass(slots=True)
class Check:
    name: str
    status: str
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "status": self.status, "detail": self.detail}


def required_bytes_per_second(sample_rate_hz: float) -> float:
    return sample_rate_hz * BYTES_PER_FRAME


def capture_size_bytes(sample_rate_hz: float, duration_seconds: float) -> float:
    return sample_rate_hz * duration_seconds * BYTES_PER_FRAME


def measure_write_throughput(
    directory: str | Path, *, megabytes: int = _DEFAULT_PROBE_MEGABYTES
) -> float:
    """Write `megabytes` of data to `directory` and return bytes/second.

    Uses `os.fsync` before stopping the clock so the number reflects data
    actually on the medium, not data parked in the page cache -- a cached
    measurement would look fast and then truncate the real capture.
    """
    destination = Path(directory).expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)
    probe_path = destination / ".dmr_surveyor_write_probe.tmp"
    block = b"\0" * (1024 * 1024)
    started = time.monotonic()
    try:
        with probe_path.open("wb") as handle:
            for _ in range(megabytes):
                handle.write(block)
            handle.flush()
            os.fsync(handle.fileno())
        elapsed = time.monotonic() - started
    finally:
        probe_path.unlink(missing_ok=True)
    if elapsed <= 0:
        raise OSError("write throughput probe completed instantly; result is not usable")
    return (megabytes * 1024 * 1024) / elapsed


def max_sustainable_sample_rate(bytes_per_second: float) -> float:
    """Highest sample rate this storage can sustain, with the safety factor
    already applied."""
    return bytes_per_second / (BYTES_PER_FRAME * _THROUGHPUT_SAFETY_FACTOR)


def _check_device(driver: str) -> Check:
    probe = probe_soapysdr(driver)
    if not probe.available:
        return Check("SDR device", FAIL, probe.probe_error or "unavailable")
    return Check("SDR device", PASS, f"found: {probe.resolved_label}")


def _check_free_space(directory: Path, needed_bytes: float) -> Check:
    try:
        usage = shutil.disk_usage(directory)
    except OSError as exc:
        return Check("Free space", FAIL, f"cannot stat {directory}: {exc}")
    needed_gib = needed_bytes / 1024**3
    free_gib = usage.free / 1024**3
    # Never fill the last of the disk: the survey stage writes spectrum
    # artifacts and the SQLite database alongside the recording.
    if usage.free < needed_bytes * 1.15:
        return Check(
            "Free space",
            FAIL,
            f"{free_gib:.2f} GiB free, capture needs ~{needed_gib:.2f} GiB plus room for artifacts",
        )
    return Check("Free space", PASS, f"{free_gib:.2f} GiB free, capture needs ~{needed_gib:.2f} GiB")


def _check_writable(directory: Path) -> Check:
    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe = directory / ".dmr_surveyor_writable.tmp"
        probe.write_bytes(b"0")
        probe.unlink()
    except OSError as exc:
        return Check("Output writable", FAIL, f"{directory}: {exc}")
    return Check("Output writable", PASS, str(directory))


def _check_throughput(directory: Path, sample_rate_hz: float, megabytes: int) -> Check:
    try:
        measured = measure_write_throughput(directory, megabytes=megabytes)
    except OSError as exc:
        return Check("Write throughput", FAIL, f"probe failed: {exc}")
    required = required_bytes_per_second(sample_rate_hz)
    measured_mb = measured / 1e6
    required_mb = required / 1e6
    ceiling_ms = max_sustainable_sample_rate(measured) / 1e6
    detail = (
        f"measured {measured_mb:.1f} MB/s, this capture needs {required_mb:.1f} MB/s "
        f"(safe up to ~{ceiling_ms:.2f} MS/s)"
    )
    if measured < required:
        return Check("Write throughput", FAIL, detail + " -- WILL drop samples")
    if measured < required * _THROUGHPUT_SAFETY_FACTOR:
        return Check("Write throughput", WARN, detail + " -- little margin")
    return Check("Write throughput", PASS, detail)


def _check_band_coverage(
    band: BandProfile, center_frequency_hz: float, sample_rate_hz: float
) -> Check:
    # Coverage is bounded by the analog IF filter, not by Nyquist. Using
    # Nyquist alone overstates it -- at 2 MS/s the filter is 1.536 MHz, so
    # 23% of the "covered" span is really in the roll-off.
    span = usable_span_hz(sample_rate_hz)
    low = center_frequency_hz - span / 2.0
    high = center_frequency_hz + span / 2.0
    covered_low = max(low, band.start_frequency_hz)
    covered_high = min(high, band.stop_frequency_hz)
    requested_width = band.stop_frequency_hz - band.start_frequency_hz
    if covered_high <= covered_low:
        return Check(
            "Band coverage",
            FAIL,
            f"tuning covers {low / 1e6:.3f}-{high / 1e6:.3f} MHz, which does not "
            f"overlap the {band.name} band ({band.start_frequency_hz / 1e6:.3f}-"
            f"{band.stop_frequency_hz / 1e6:.3f} MHz)",
        )
    covered_fraction = (covered_high - covered_low) / requested_width
    detail = (
        f"usable span {low / 1e6:.3f}-{high / 1e6:.3f} MHz "
        f"(IF filter {sdrplay_if_bandwidth_hz(sample_rate_hz) / 1e6:.3f} MHz); "
        f"{covered_fraction * 100:.0f}% of the {band.name} band "
        f"({band.start_frequency_hz / 1e6:.3f}-{band.stop_frequency_hz / 1e6:.3f} MHz)"
    )
    if covered_fraction < 0.999:
        return Check(
            "Band coverage",
            WARN,
            detail + " -- the rest is reported as uncovered, not silently skipped",
        )
    return Check("Band coverage", PASS, detail)


def _check_rate_efficiency(sample_rate_hz: float) -> Check:
    """Flag sample rates that cost storage bandwidth without buying spectrum.

    The IF filter steps from 1.536 MHz straight to 5 MHz, so 3 or 4 MS/s
    writes 1.5-2x as much data as 2 MS/s for exactly the same usable span.
    """
    bandwidth = sdrplay_if_bandwidth_hz(sample_rate_hz)
    if sample_rate_hz <= bandwidth * 1.05:
        return Check(
            "Rate efficiency",
            PASS,
            f"{sample_rate_hz / 1e6:.3f} MS/s is well matched to its "
            f"{bandwidth / 1e6:.3f} MHz IF filter",
        )
    # Largest rate that yields the same filter, i.e. the wasted headroom.
    return Check(
        "Rate efficiency",
        WARN,
        f"{sample_rate_hz / 1e6:.3f} MS/s still gets only a {bandwidth / 1e6:.3f} MHz IF filter, "
        f"so it writes {sample_rate_hz / max(bandwidth, 1.0):.1f}x the data for no extra usable "
        "spectrum. Drop to just above the filter width, or step up to the next filter "
        "(5 MS/s gives 5 MHz).",
    )


def _check_gps(gps_url: str | None, timeout_seconds: float) -> Check:
    if not gps_url:
        return Check("GPS", WARN, "no --gps-url given; runs will record gps_source=not_configured")
    try:
        fix = fetch_gps_fix(gps_url, timeout_seconds=timeout_seconds)
    except GpsFixError as exc:
        return Check("GPS", WARN, f"{exc} -- captures still work, without coordinates")
    accuracy = f", accuracy ~{fix.accuracy_m:.0f} m" if fix.accuracy_m is not None else ""
    return Check("GPS", PASS, f"{fix.latitude:.6f}, {fix.longitude:.6f}{accuracy}")


def run_preflight(
    output_dir: str | Path,
    *,
    band: BandProfile,
    center_frequency_hz: float,
    sample_rate_hz: float,
    duration_seconds: float,
    driver: str = "sdrplay",
    gps_url: str | None = None,
    gps_timeout_seconds: float = 10.0,
    probe_megabytes: int = _DEFAULT_PROBE_MEGABYTES,
    skip_throughput: bool = False,
) -> dict[str, Any]:
    """Run every pre-capture check and return the results plus a verdict.

    The verdict is `fail` if any check failed, `warn` if any warned, else
    `pass`. A warning never means "do not capture" -- it means the operator
    should know something before committing to the recording.
    """
    destination = Path(output_dir).expanduser().resolve()
    needed = capture_size_bytes(sample_rate_hz, duration_seconds)

    checks = [_check_device(driver), _check_writable(destination)]
    if checks[-1].status == PASS:
        checks.append(_check_free_space(destination, needed))
        if skip_throughput:
            checks.append(Check("Write throughput", WARN, "skipped (--skip-throughput)"))
        else:
            checks.append(_check_throughput(destination, sample_rate_hz, probe_megabytes))
    checks.append(_check_band_coverage(band, center_frequency_hz, sample_rate_hz))
    checks.append(_check_rate_efficiency(sample_rate_hz))
    checks.append(_check_gps(gps_url, gps_timeout_seconds))

    statuses = {check.status for check in checks}
    verdict = FAIL if FAIL in statuses else (WARN if WARN in statuses else PASS)
    return {
        "verdict": verdict,
        "checks": [check.to_dict() for check in checks],
        "capture_size_bytes": needed,
        "required_bytes_per_second": required_bytes_per_second(sample_rate_hz),
    }


__all__ = [
    "BYTES_PER_FRAME",
    "FAIL",
    "PASS",
    "WARN",
    "Check",
    "capture_size_bytes",
    "max_sustainable_sample_rate",
    "measure_write_throughput",
    "required_bytes_per_second",
    "run_preflight",
    "sdrplay_if_bandwidth_hz",
    "usable_span_hz",
]
