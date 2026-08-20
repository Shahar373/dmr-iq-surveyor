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
    nyquist_low = center_frequency_hz - sample_rate_hz / 2.0
    nyquist_high = center_frequency_hz + sample_rate_hz / 2.0
    covered_low = max(nyquist_low, band.start_frequency_hz)
    covered_high = min(nyquist_high, band.stop_frequency_hz)
    requested_width = band.stop_frequency_hz - band.start_frequency_hz
    if covered_high <= covered_low:
        return Check(
            "Band coverage",
            FAIL,
            f"tuning covers {nyquist_low / 1e6:.3f}-{nyquist_high / 1e6:.3f} MHz, which does not "
            f"overlap the {band.name} band ({band.start_frequency_hz / 1e6:.3f}-"
            f"{band.stop_frequency_hz / 1e6:.3f} MHz)",
        )
    covered_fraction = (covered_high - covered_low) / requested_width
    detail = (
        f"tuning covers {nyquist_low / 1e6:.3f}-{nyquist_high / 1e6:.3f} MHz; "
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
]
