"""Phase 6A discovery: wideband IQ -> objective, protocol-agnostic RF observations.

This module never assumes a frequency list. It measures the usable passband
of the recording, analyzes the capture in bounded time segments (so runtime
and memory stay bounded on long files), detects candidates independently in
each segment using the existing Phase 3 detector engine, and aggregates them
into `RfObservation` records with honestly-defined occupancy and persistence.

No protocol classification happens here. `classification` is always
`"unknown"` with `classification_method="spectral_only"` -- Phase 6B is
where decoder evidence can promote a candidate to a protocol-specific label.
`spectral_class` carries the spectral-shape hypothesis alone and must never
be read as a protocol confirmation.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

from dmr_iq_surveyor.detect.core import DetectionSettings, nearest_raster_hz
from dmr_iq_surveyor.detect.features import detect_from_data, feature_at
from dmr_iq_surveyor.detect.merge import merge_recordings
from dmr_iq_surveyor.iq.metadata import inspect_wave_iq
from dmr_iq_surveyor.iq.reader import IQMemmapReader
from dmr_iq_surveyor.models import RecordingInfo
from dmr_iq_surveyor.spectrum.core import (
    SpectrumSettings,
    build_window,
    fft_frame_count,
    frequency_axis_hz,
    iter_fft_starts,
    local_noise_floor_db,
    percentile_frame_indices,
    periodogram_power_density,
    power_to_db,
)
from dmr_iq_surveyor.survey.profiles import BandProfile

POWER_UNIT_DBFS_PER_HZ = "dbfs_per_hz"

_FILENAME_TIMESTAMP_RE = re.compile(r"(?:^|[_-])(\d{8})_(\d{6})(?:[_-]|$)")


@dataclass(slots=True)
class Segment:
    index: int
    start_frame: int
    frame_count: int
    start_seconds: float
    end_seconds: float


def plan_segments(
    total_frames: int,
    sample_rate_hz: float,
    *,
    segment_seconds: float | None,
    stride_seconds: float | None,
    max_segments: int | None,
) -> list[Segment]:
    """Plan bounded time segments to analyze.

    When `segment_seconds`/`stride_seconds` are None, the whole recording is
    a single segment (equivalent to the unsegmented Phase 2 behaviour).
    """
    if segment_seconds is None or stride_seconds is None:
        return [
            Segment(
                index=0,
                start_frame=0,
                frame_count=total_frames,
                start_seconds=0.0,
                end_seconds=total_frames / sample_rate_hz if sample_rate_hz else 0.0,
            )
        ]
    segment_frames = max(1, round(segment_seconds * sample_rate_hz))
    stride_frames = max(1, round(stride_seconds * sample_rate_hz))
    segments: list[Segment] = []
    start = 0
    index = 0
    while start < total_frames:
        if max_segments is not None and index >= max_segments:
            break
        count = min(segment_frames, total_frames - start)
        segments.append(
            Segment(
                index=index,
                start_frame=start,
                frame_count=count,
                start_seconds=start / sample_rate_hz,
                end_seconds=(start + count) / sample_rate_hz,
            )
        )
        index += 1
        start += stride_frames
    return segments


def _capture_time_from_filename(path: Path) -> str | None:
    match = _FILENAME_TIMESTAMP_RE.search(path.name)
    if match is None:
        return None
    date_part, time_part = match.groups()
    try:
        value = datetime.strptime(date_part + time_part, "%Y%m%d%H%M%S").replace(tzinfo=UTC)
    except ValueError:
        return None
    return value.isoformat()


def resolve_capture_time(
    info: RecordingInfo,
    *,
    user_capture_start_utc: str | None = None,
) -> tuple[str | None, str]:
    """Resolve the RF capture time and its provenance.

    Never falls back to import time or run ID ordering: the returned source
    is one of `user`, `auxi`, `filename`, `unknown`, and callers must treat
    `unknown` as excluded from first/last-seen computation, not as "now".
    """
    if user_capture_start_utc:
        return user_capture_start_utc, "user"
    if info.auxi is not None and info.auxi.start_time_utc:
        return info.auxi.start_time_utc, "auxi"
    filename_time = _capture_time_from_filename(Path(info.path))
    if filename_time is not None:
        return filename_time, "filename"
    return None, "unknown"


@dataclass(slots=True)
class SegmentSpectrum:
    """The per-segment products needed for detection, kept in memory only
    for the duration of one survey run (never all of the source IQ)."""

    frequency_hz: np.ndarray
    average_db: np.ndarray
    percentile_db: np.ndarray
    noise_db: np.ndarray
    occupancy_pct: np.ndarray
    edge_mask: np.ndarray
    dc_mask: np.ndarray
    fft_count: int

    def as_data(self) -> dict[str, Any]:
        return {
            "frequency_hz": self.frequency_hz,
            "average_db": self.average_db,
            "percentile_db": self.percentile_db,
            "noise_db": self.noise_db,
            "occupancy_pct": self.occupancy_pct,
            "edge_mask": self.edge_mask,
            "dc_mask": self.dc_mask,
        }


def accumulate_segment_spectrum(
    frames: Iterable[np.ndarray],
    *,
    fft_count: int,
    sample_rate_hz: float,
    center_frequency_hz: float,
    nominal_low_hz: float,
    nominal_high_hz: float,
    settings: SpectrumSettings,
) -> SegmentSpectrum | None:
    """Reduce a run of FFT-length sample frames to one `SegmentSpectrum`.

    Split out of `analyze_segment` so live streaming and offline file
    analysis share one implementation instead of two that drift. `frames` is
    consumed lazily, which is what preserves the offline path's memmap
    discipline: it hands over a generator that reads one FFT window at a
    time, never a whole segment at once.
    """
    if fft_count < 1:
        return None
    frequency_hz = frequency_axis_hz(center_frequency_hz, sample_rate_hz, settings.fft_size)
    sample_rate = sample_rate_hz
    count = fft_count
    resolution_hz = sample_rate / settings.fft_size
    window = build_window(settings.window, settings.fft_size)
    average_sum = np.zeros(settings.fft_size, dtype=np.float64)
    noise_sum_db = np.zeros(settings.fft_size, dtype=np.float64)
    occupied = np.zeros(settings.fft_size, dtype=np.uint32)
    percentile_indices = percentile_frame_indices(count, settings.percentile_max_frames)
    percentile_lookup = {int(frame): row for row, frame in enumerate(percentile_indices)}
    percentile_samples_db = np.empty((len(percentile_indices), settings.fft_size), dtype=np.float32)
    bins_per_noise_window = max(1, round(settings.local_noise_window_hz / resolution_hz))

    for frame_index, samples in enumerate(frames):
        power = periodogram_power_density(samples, window, sample_rate)
        spectrum_db = power_to_db(power)
        floor_db = local_noise_floor_db(spectrum_db, bins_per_noise_window)
        average_sum += power
        noise_sum_db += floor_db
        occupied += spectrum_db > (floor_db + settings.occupancy_threshold_db)
        row = percentile_lookup.get(frame_index)
        if row is not None:
            percentile_samples_db[row] = spectrum_db.astype(np.float32)

    average_db = power_to_db(average_sum / count).astype(np.float32)
    noise_db = (noise_sum_db / count).astype(np.float32)
    occupancy_pct = (occupied.astype(np.float64) * (100.0 / count)).astype(np.float32)
    percentile_db = np.percentile(percentile_samples_db, settings.percentile, axis=0).astype(
        np.float32
    )

    edge_mask = (frequency_hz < nominal_low_hz + settings.edge_exclusion_hz) | (
        frequency_hz > nominal_high_hz - settings.edge_exclusion_hz
    )
    dc_mask = np.abs(frequency_hz - center_frequency_hz) <= settings.dc_exclusion_hz

    return SegmentSpectrum(
        frequency_hz=frequency_hz,
        average_db=average_db,
        percentile_db=percentile_db,
        noise_db=noise_db,
        occupancy_pct=occupancy_pct,
        edge_mask=edge_mask,
        dc_mask=dc_mask,
        fft_count=count,
    )


def analyze_segment(
    reader: IQMemmapReader,
    segment: Segment,
    *,
    settings: SpectrumSettings,
) -> SegmentSpectrum | None:
    """Analyze one time segment using the same FFT/PSD primitives as Phase 2,
    scoped to `segment`'s frame range. Returns None when the segment is too
    short to contain a full FFT window (typically only the final segment).
    """
    info = reader.info
    sample_rate = float(info.fmt.sample_rate_hz)
    count = fft_frame_count(segment.frame_count, settings.fft_size, settings.overlap_ratio)
    if count < 1:
        return None

    def frames() -> Iterator[np.ndarray]:
        for relative_start in iter_fft_starts(
            segment.frame_count, settings.fft_size, settings.overlap_ratio
        ):
            yield reader.read_complex(segment.start_frame + relative_start, settings.fft_size)

    return accumulate_segment_spectrum(
        frames(),
        fft_count=count,
        sample_rate_hz=sample_rate,
        center_frequency_hz=float(info.center_frequency_hz),
        nominal_low_hz=float(info.nominal_frequency_low_hz),
        nominal_high_hz=float(info.nominal_frequency_high_hz),
        settings=settings,
    )


@dataclass(slots=True)
class UsablePassband:
    usable_low_hz: float
    usable_high_hz: float
    coverage_status: str  # complete | partial | unknown
    reference_level_db: float
    rolloff_db: float
    uncovered_ranges_hz: list[tuple[float, float]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["uncovered_ranges_hz"] = [list(pair) for pair in self.uncovered_ranges_hz]
        return payload


def measure_usable_passband(
    segments: list[SegmentSpectrum],
    *,
    requested_low_hz: float,
    requested_high_hz: float,
    rolloff_db: float,
    local_noise_window_hz: float = 200_000.0,
) -> UsablePassband:
    """Measure where the receiver's response rolls off, rather than assuming
    the full Nyquist width is usable RF bandwidth.

    Heuristic: build a weighted-mean aggregate PSD across segments, smooth it
    with the same windowed-median used for the noise floor (amplitude
    invariant to narrow signal peaks/nulls), then walk outward from the
    center to find where the smoothed curve first drops `rolloff_db` below
    the passband median and does not recover. This is an approximation of
    the receiver/decimation-filter roll-off shape, not a calibrated
    measurement, and is reported alongside its inputs for transparency.
    """
    if not segments:
        return UsablePassband(
            usable_low_hz=requested_low_hz,
            usable_high_hz=requested_high_hz,
            coverage_status="unknown",
            reference_level_db=float("nan"),
            rolloff_db=rolloff_db,
        )
    frequency_hz = segments[0].frequency_hz
    weights = np.array([seg.fft_count for seg in segments], dtype=np.float64)
    stacked = np.vstack([seg.average_db.astype(np.float64) for seg in segments])
    aggregate_db = np.average(stacked, axis=0, weights=weights)
    resolution_hz = float(np.median(np.diff(frequency_hz)))
    bins_per_window = max(1, round(local_noise_window_hz / resolution_hz))
    smoothed = local_noise_floor_db(aggregate_db, bins_per_window)
    reference_level = float(np.median(smoothed))

    center_index = int(np.argmin(np.abs(frequency_hz - float(np.median(frequency_hz)))))
    threshold = reference_level - rolloff_db

    def _walk(indices: range) -> int:
        last_good = center_index
        for idx in indices:
            if smoothed[idx] >= threshold:
                last_good = idx
            else:
                break
        return last_good

    low_index = _walk(range(center_index, -1, -1))
    high_index = _walk(range(center_index, len(smoothed)))
    usable_low_hz = float(frequency_hz[low_index])
    usable_high_hz = float(frequency_hz[high_index])

    uncovered: list[tuple[float, float]] = []
    if usable_low_hz > requested_low_hz:
        uncovered.append((requested_low_hz, usable_low_hz))
    if usable_high_hz < requested_high_hz:
        uncovered.append((usable_high_hz, requested_high_hz))
    coverage_status = "complete" if not uncovered else "partial"

    return UsablePassband(
        usable_low_hz=usable_low_hz,
        usable_high_hz=usable_high_hz,
        coverage_status=coverage_status,
        reference_level_db=reference_level,
        rolloff_db=rolloff_db,
        uncovered_ranges_hz=uncovered,
    )


@dataclass(slots=True)
class RfObservation:
    measured_center_hz: float
    bandwidth_hz: float
    peak_dbfs_per_hz: float
    average_dbfs_per_hz: float
    noise_floor_dbfs_per_hz: float
    power_unit: str
    calibrated: bool
    snr_db: float
    p95_snr_db: float
    peak_concentration_db: float
    occupancy_pct: float
    occupancy_threshold_db: float
    occupancy_sample_count: int
    persistence: float
    segments_detected: int
    segments_analyzed: int
    equivalent_width_hz: float
    spectral_fill: float
    symmetry: float
    nearest_raster_hz: float
    raster_spacing_hz: float
    raster_error_hz: float
    spectral_class: str
    classification: str
    classification_confidence: float
    classification_method: str
    edge_warning: bool
    dc_warning: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _nearest_raster(measured_center_hz: float, raster_spacings_hz: list[float]) -> tuple[float, float]:
    """Pick the raster spacing (from the band profile's candidates) that
    yields the smallest snap error, and return (nearest_hz, spacing_hz).
    Evidence only -- never used to move the measured center."""
    best_hz = measured_center_hz
    best_spacing = raster_spacings_hz[0]
    best_error = float("inf")
    for spacing in raster_spacings_hz:
        candidate = nearest_raster_hz(measured_center_hz, spacing)
        error = abs(measured_center_hz - candidate)
        if error < best_error:
            best_error = error
            best_hz = candidate
            best_spacing = spacing
    return best_hz, best_spacing


@dataclass(slots=True)
class _ChannelRemeasurement:
    occupancy_pct: float
    occupancy_sample_count: int
    peak_dbfs_per_hz: float
    average_dbfs_per_hz: float
    noise_floor_dbfs_per_hz: float


def _channel_power_dbfs_per_hz(
    data: dict[str, Any], center_hz: float, width_hz: float
) -> tuple[float, float, float] | None:
    """Absolute (relative-to-full-scale) power stats for one segment's
    channel window. `feature_at` only returns SNR ratios; this is the
    absolute-power counterpart needed for `peak/average/noise_floor_dbfs_per_hz`.
    """
    frequency = data["frequency_hz"]
    half_width = max(width_hz / 2.0, 1.0)
    mask = np.abs(frequency - center_hz) <= half_width
    if not np.any(mask):
        return None
    average_linear = np.power(10.0, data["average_db"][mask].astype(np.float64) / 10.0)
    noise_linear = np.power(10.0, data["noise_db"][mask].astype(np.float64) / 10.0)
    peak_db = float(np.max(data["percentile_db"][mask]))
    average_db = 10.0 * float(np.log10(float(np.mean(average_linear)) + 1e-300))
    noise_floor_db = 10.0 * float(np.log10(float(np.mean(noise_linear)) + 1e-300))
    return peak_db, average_db, noise_floor_db


def _remeasure_across_segments(
    merged_candidate: dict[str, Any],
    segment_data: list[dict[str, Any]],
    segment_fft_counts: list[int],
    settings: DetectionSettings,
) -> _ChannelRemeasurement:
    """Re-measure occupancy and absolute power at the merged candidate's
    canonical frequency against *every* analyzed segment (not just the ones
    where it emerged as a local-maximum candidate), so `occupancy_pct`
    reflects the whole analyzed capture rather than only the segments that
    happened to independently detect it.
    """
    frequency = float(merged_candidate["measured_center_hz"])
    width_hz = float(merged_candidate["width_90_hz"])
    occupancy_weighted_sum = 0.0
    occupancy_weight_total = 0
    peak_values: list[float] = []
    average_values: list[float] = []
    noise_values: list[float] = []
    weight_for_average: list[int] = []
    for data, fft_count in zip(segment_data, segment_fft_counts, strict=True):
        feature = feature_at(data, frequency, settings)
        power = _channel_power_dbfs_per_hz(data, frequency, width_hz)
        if feature is not None:
            occupancy_weighted_sum += feature["occupancy_pct"] * fft_count
            occupancy_weight_total += fft_count
        if power is not None:
            peak_db, average_db, noise_db = power
            peak_values.append(peak_db)
            average_values.append(average_db * fft_count)
            noise_values.append(noise_db * fft_count)
            weight_for_average.append(fft_count)
    occupancy_pct = (
        occupancy_weighted_sum / occupancy_weight_total if occupancy_weight_total > 0 else 0.0
    )
    weight_total = sum(weight_for_average)
    average_dbfs_per_hz = (
        sum(average_values) / weight_total if weight_total > 0 else float("nan")
    )
    noise_floor_dbfs_per_hz = (
        sum(noise_values) / weight_total if weight_total > 0 else float("nan")
    )
    peak_dbfs_per_hz = max(peak_values) if peak_values else float("nan")
    return _ChannelRemeasurement(
        occupancy_pct=occupancy_pct,
        occupancy_sample_count=occupancy_weight_total,
        peak_dbfs_per_hz=peak_dbfs_per_hz,
        average_dbfs_per_hz=average_dbfs_per_hz,
        noise_floor_dbfs_per_hz=noise_floor_dbfs_per_hz,
    )


def observations_from_segments(
    segment_spectra: list[SegmentSpectrum],
    *,
    band_profile: BandProfile,
    center_frequency_hz: float,
    sample_rate_hz: float,
    spectrum_settings: SpectrumSettings,
) -> dict[str, Any]:
    """Turn analysed segments into detected observations.

    The whole detector lives here: usable-passband measurement,
    per-segment detection, cross-segment merging and remeasurement,
    raster snapping. It needs nothing but the segments themselves.
    Separating it from the file reading is what lets a live stream
    produce observations through exactly this code, rather than through
    a second implementation that would drift from it.

    Nothing here knows about reference data. Segments are measured on
    the band profile's own raster; which site a frequency might belong
    to is decided afterwards, elsewhere.
    """
    passband = measure_usable_passband(
        segment_spectra,
        requested_low_hz=band_profile.start_frequency_hz,
        requested_high_hz=band_profile.stop_frequency_hz,
        rolloff_db=band_profile.usable_passband_rolloff_db,
    )

    detection_settings = band_profile.detection_settings()
    recording_dict = {
        "center_frequency_hz": center_frequency_hz,
        "sample_rate_hz": sample_rate_hz,
    }
    segment_results: list[tuple[str, dict[str, Any]]] = []
    segment_data_list: list[dict[str, Any]] = []
    for segment_index, spectrum in enumerate(segment_spectra):
        data = spectrum.as_data()
        segment_data_list.append(data)
        result = detect_from_data(
            data,
            detection_settings,
            recording=recording_dict,
            source_label=f"segment_{segment_index:04d}",
            scan_low_hz=band_profile.start_frequency_hz,
            scan_high_hz=band_profile.stop_frequency_hz,
        )
        segment_results.append((f"segment_{segment_index:04d}", result))

    merged_candidates = merge_recordings(segment_results, detection_settings)

    observations: list[RfObservation] = []
    segment_fft_counts = [spectrum.fft_count for spectrum in segment_spectra]
    for candidate in merged_candidates:
        remeasured = _remeasure_across_segments(
            candidate, segment_data_list, segment_fft_counts, detection_settings
        )
        measured_center = float(candidate["measured_center_hz"])
        nearest_hz, spacing_hz = _nearest_raster(measured_center, band_profile.raster_spacings_hz)
        observations.append(
            RfObservation(
                measured_center_hz=measured_center,
                bandwidth_hz=float(candidate["width_90_hz"]),
                peak_dbfs_per_hz=remeasured.peak_dbfs_per_hz,
                average_dbfs_per_hz=remeasured.average_dbfs_per_hz,
                noise_floor_dbfs_per_hz=remeasured.noise_floor_dbfs_per_hz,
                power_unit=POWER_UNIT_DBFS_PER_HZ,
                calibrated=False,
                snr_db=float(candidate["average_snr_db"]),
                p95_snr_db=float(candidate["p95_snr_db"]),
                peak_concentration_db=float(candidate["peak_to_channel_mean_db"]),
                occupancy_pct=remeasured.occupancy_pct,
                occupancy_threshold_db=spectrum_settings.occupancy_threshold_db,
                occupancy_sample_count=remeasured.occupancy_sample_count,
                persistence=float(candidate["confidence_components"]["persistence"]),
                segments_detected=int(candidate["recordings_seen"]),
                segments_analyzed=len(segment_spectra),
                equivalent_width_hz=float(candidate["equivalent_width_hz"]),
                spectral_fill=float(candidate["spectral_fill_ratio"]),
                symmetry=float(candidate["symmetry_score"]),
                nearest_raster_hz=nearest_hz,
                raster_spacing_hz=spacing_hz,
                raster_error_hz=measured_center - nearest_hz,
                spectral_class=str(candidate["spectral_class"]),
                classification="unknown",
                classification_confidence=float(candidate["confidence"]),
                classification_method="spectral_only",
                edge_warning=bool(candidate["edge_warning"]),
                dc_warning=bool(candidate["dc_warning"]),
            )
        )
    observations.sort(key=lambda item: item.measured_center_hz)

    return {
        "observations": observations,
        "usable_passband": passband,
        "segments_analyzed": len(segment_spectra),
        "detection_settings": detection_settings,
        "occupancy_threshold_db": spectrum_settings.occupancy_threshold_db,
    }


def discover_observations(
    recording_path: str | Path,
    *,
    band_profile: BandProfile,
    assumed_iq_order: str = "IQ",
    spectrum_fft_size: int = 65_536,
    spectrum_overlap_ratio: float = 0.5,
) -> dict[str, Any]:
    """Run the full Phase 6A discovery pass on one wideband IQ recording.

    Returns a dict with `observations` (list[RfObservation]), the recording
    info, the measured usable passband, and per-segment bookkeeping needed
    by the survey pipeline (segment count, analyzed seconds).
    """
    source = Path(recording_path).expanduser().resolve()
    info = inspect_wave_iq(source, assumed_iq_order=assumed_iq_order)
    if info.center_frequency_hz is None:
        raise ValueError("Center frequency is required for survey discovery")
    reader = IQMemmapReader(info)
    sample_rate = float(info.fmt.sample_rate_hz)

    # occupancy_threshold_db here is the spectrum-level "energy above local
    # noise floor" threshold (matches the Phase 2 default), independent of
    # the detector's own SNR gates in DetectionSettings.
    spectrum_settings = SpectrumSettings(
        fft_size=spectrum_fft_size,
        overlap_ratio=spectrum_overlap_ratio,
    )

    segments = plan_segments(
        info.frame_count,
        sample_rate,
        segment_seconds=band_profile.segment_seconds,
        stride_seconds=band_profile.segment_stride_seconds,
        max_segments=band_profile.max_segments,
    )

    segment_spectra: list[SegmentSpectrum] = []
    skipped_segments = 0
    for segment in segments:
        result = analyze_segment(reader, segment, settings=spectrum_settings)
        if result is None:
            skipped_segments += 1
            continue
        segment_spectra.append(result)

    detection = observations_from_segments(
        segment_spectra,
        band_profile=band_profile,
        center_frequency_hz=float(info.center_frequency_hz),
        sample_rate_hz=sample_rate,
        spectrum_settings=spectrum_settings,
    )
    return {
        **detection,
        "recording": info,
        "segment_count": len(segments),
        "segments_skipped": skipped_segments,
        "analyzed_seconds": sum(
            seg.end_seconds - seg.start_seconds
            for seg in segments[: len(segment_spectra)]
        ),
    }


__all__ = [
    "POWER_UNIT_DBFS_PER_HZ",
    "RfObservation",
    "Segment",
    "SegmentSpectrum",
    "UsablePassband",
    "accumulate_segment_spectrum",
    "analyze_segment",
    "discover_observations",
    "measure_usable_passband",
    "observations_from_segments",
    "plan_segments",
    "resolve_capture_time",
]
