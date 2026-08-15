"""Live SDRplay capture orchestration: open device -> stream to WAV ->
optionally hand the result straight to `survey.pipeline.run_survey()`.

This module exists as one explicit, one-off exception to this project's
documented "no premature live acquisition" principle (see `CLAUDE.md`),
authorized for a single field-recording request -- a single command that
captures AND analyzes. It reuses `run_survey()` unchanged rather than
reimplementing any part of Phase 6A.
"""

from __future__ import annotations

import json
import platform
import resource
import sys
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

from dmr_iq_surveyor import __version__
from dmr_iq_surveyor.capture.device import DeviceSettings, IqDevice, SoapyIqDevice
from dmr_iq_surveyor.capture.wav_writer import WaveIQWriter, WaveIQWriterSettings
from dmr_iq_surveyor.survey.pipeline import DEFAULT_DATABASE_PATH, run_survey
from dmr_iq_surveyor.survey.profiles import BandProfile, SiteProfile

_DEFAULT_CHUNK_FRAMES = 262_144


def _peak_rss_bytes() -> int:
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(value * 1024 if sys.platform != "darwin" else value)


@dataclass(slots=True)
class CaptureSettings:
    center_frequency_hz: float
    sample_rate_hz: float
    duration_seconds: float
    gain_db: float | None = None
    agc: bool = False
    antenna: str | None = None
    driver: str = "sdrplay"
    channel: int = 0
    chunk_frames: int = _DEFAULT_CHUNK_FRAMES
    write_auxi: bool = True

    def validate(self) -> None:
        if self.center_frequency_hz <= 0:
            raise ValueError("center_frequency_hz must be positive")
        if self.sample_rate_hz <= 0:
            raise ValueError("sample_rate_hz must be positive")
        if self.duration_seconds <= 0:
            raise ValueError("duration_seconds must be positive")
        if self.chunk_frames <= 0:
            raise ValueError("chunk_frames must be positive")
        if self.agc and self.gain_db is not None:
            raise ValueError("gain_db must not be set when agc is enabled")
        if not self.agc and self.gain_db is None:
            raise ValueError("gain_db is required when agc is disabled (AGC off requires a manual gain)")

    def to_device_settings(self) -> DeviceSettings:
        return DeviceSettings(
            driver=self.driver,
            sample_rate_hz=self.sample_rate_hz,
            center_frequency_hz=self.center_frequency_hz,
            gain_db=self.gain_db,
            agc=self.agc,
            antenna=self.antenna,
            channel=self.channel,
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def sdrconnect_style_filename(center_frequency_hz: float, moment: datetime | None = None) -> str:
    """SDRconnect-style filename: `SDRconnect_IQ_<YYYYMMDD>_<HHMMSS>_<centerHz>HZ.wav`.

    Matches the pattern `survey/discovery.py::resolve_capture_time()`'s
    filename fallback parses (`_FILENAME_TIMESTAMP_RE`) and the frequency
    suffix `iq/metadata.py::_center_frequency_from_filename()` parses --
    kept correct even when `write_auxi=False` leaves that fallback as the
    only source of capture time/frequency.
    """
    when = moment or datetime.now(UTC)
    return f"SDRconnect_IQ_{when.strftime('%Y%m%d_%H%M%S')}_{round(center_frequency_hz)}HZ.wav"


def run_capture(
    output_dir: str | Path,
    *,
    settings: CaptureSettings,
    device: IqDevice | None = None,
    filename: str | None = None,
) -> dict[str, Any]:
    """Capture `settings.duration_seconds` of IQ into a new WAV file under
    `output_dir` and return a manifest describing it.

    Streams from the device in `settings.chunk_frames`-sized chunks straight
    to `WaveIQWriter` -- the full capture is never held in memory, matching
    this project's `iq/reader.py` streaming discipline applied to writing.
    `device` defaults to a real `SoapyIqDevice`; tests pass a synthetic stub
    implementing the same `IqDevice` interface instead.
    """
    started = time.time()
    settings.validate()
    destination = Path(output_dir).expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)

    resolved_device = device or SoapyIqDevice()
    device_settings = settings.to_device_settings()
    resolved_device.open(device_settings)

    target_frame_count = round(settings.duration_seconds * settings.sample_rate_hz)
    name = filename or sdrconnect_style_filename(settings.center_frequency_hz)
    wav_path = destination / name

    writer_settings = WaveIQWriterSettings(
        sample_rate_hz=round(settings.sample_rate_hz),
        center_frequency_hz=round(settings.center_frequency_hz),
        write_auxi=settings.write_auxi,
    )
    writer = WaveIQWriter(wav_path, writer_settings)
    try:
        while writer.frame_count < target_frame_count:
            remaining = target_frame_count - writer.frame_count
            chunk = resolved_device.read_stream_chunk(min(settings.chunk_frames, remaining))
            chunk = np.asarray(chunk)
            if chunk.size == 0:
                continue
            if chunk.size > remaining:
                chunk = chunk[:remaining]
            writer.write_frames(chunk)
    finally:
        resolved_device.close()
        writer_summary = writer.close()

    elapsed = time.time() - started
    manifest = {
        "tool": "dmr-iq-surveyor",
        "tool_version": __version__,
        "wav_path": str(wav_path),
        "settings": settings.to_dict(),
        "frame_count": writer_summary["frame_count"],
        "requested_frame_count": target_frame_count,
        "requested_duration_seconds": settings.duration_seconds,
        "actual_duration_seconds": writer_summary["frame_count"] / settings.sample_rate_hz,
        "start_utc": writer_summary["start_utc"],
        "stop_utc": writer_summary["stop_utc"],
        "elapsed_seconds": elapsed,
        "peak_rss_bytes": _peak_rss_bytes(),
        "python": platform.python_version(),
    }
    (destination / f"{wav_path.stem}_capture_report.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )
    return manifest


def run_capture_and_survey(
    recording_output_dir: str | Path,
    survey_output_dir: str | Path,
    *,
    capture: CaptureSettings,
    band: str | Path | BandProfile,
    site: str | Path | SiteProfile,
    device: IqDevice | None = None,
    filename: str | None = None,
    run_id: str | None = None,
    database_path: str | Path | None = None,
    assumed_iq_order: str = "IQ",
    compute_source_hash: bool = False,
    spectrum_fft_size: int = 65_536,
    spectrum_overlap_ratio: float = 0.5,
) -> dict[str, Any]:
    """Capture live IQ and immediately run the existing `survey run` pipeline
    on the result -- the single command this module was built for. Reuses
    `survey.pipeline.run_survey()` unchanged."""
    capture_manifest = run_capture(
        recording_output_dir,
        settings=capture,
        device=device,
        filename=filename,
    )
    survey_result = run_survey(
        capture_manifest["wav_path"],
        survey_output_dir,
        band=band,
        site=site,
        run_id=run_id,
        database_path=database_path or DEFAULT_DATABASE_PATH,
        assumed_iq_order=assumed_iq_order,
        compute_source_hash=compute_source_hash,
        spectrum_fft_size=spectrum_fft_size,
        spectrum_overlap_ratio=spectrum_overlap_ratio,
    )
    return {"capture": capture_manifest, "survey": survey_result}


__all__ = [
    "CaptureSettings",
    "run_capture",
    "run_capture_and_survey",
    "sdrconnect_style_filename",
]
