"""A moving survey: stream, measure, bin, and let the existing solver work.

The stationary mode records a stop to a WAV and analyses it afterwards.
This mode never writes IQ at all. It reads the SDR continuously, reduces
each window of samples to a spectrum, tags it with where the receiver was,
and drops the samples. What reaches storage is what already reaches it
today: `survey_runs` rows and their `rf_observations`, one virtual run per
spatial bin.

That is the whole design. Because a bin is written as an ordinary survey
run, `materialise_measurements()` and `solve_all_sites()` consume a drive
without knowing one happened, and so do the stop list, the exclusions, the
common-mode correction and the next-stop planner.

Discovery still comes before reference. Windows are measured across the
band profile's own raster by `observations_from_segments()` -- the same
detector the offline pass runs -- and no frequency list is consulted here.
Which site a detected frequency might belong to is decided afterwards, by
`geo/measurements.py`, exactly as it is for a recorded stop.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

from dmr_iq_surveyor import __version__
from dmr_iq_surveyor.capture.device import DeviceSettings, IqDevice, SoapyIqDevice
from dmr_iq_surveyor.live.bins import DEFAULT_BIN_SIZE_M, BinGrid, BinVisit
from dmr_iq_surveyor.spectrum.core import SpectrumSettings, fft_frame_count
from dmr_iq_surveyor.survey.discovery import (
    accumulate_segment_spectrum,
    observations_from_segments,
)
from dmr_iq_surveyor.survey.profiles import BandProfile, SiteProfile
from dmr_iq_surveyor.survey.store import SurveyRunRecord, import_survey_run, upsert_site

# Why a window is a second: at 50 km/h it covers 14 m, which is about 40
# wavelengths at 868 MHz -- the drive-test convention for averaging out fast
# fading to recover the local mean. Shorter and multipath leaks into the
# measurement; much longer and one window smears across bins.
DEFAULT_WINDOW_SECONDS = 1.0


@dataclass(slots=True)
class Position:
    latitude: float
    longitude: float
    accuracy_m: float | None = None
    at: float = 0.0  # monotonic seconds, for staleness


@dataclass(slots=True)
class LiveSettings:
    band: str = "central_800_narrow"
    site_id: str = "mobile"
    center_frequency_hz: float = 867_406_250.0
    sample_rate_hz: float = 5_000_000.0
    if_gain_reduction_db: float = 26.0
    lna_state: int = 8
    driver: str = "sdrplay"
    window_seconds: float = DEFAULT_WINDOW_SECONDS
    bin_size_m: float = DEFAULT_BIN_SIZE_M
    # Origin of the bin grid. A campaign constant, not a per-drive value:
    # bin indices only mean something relative to it, so two drives anchored
    # differently would give the same street different ids and write the
    # near-duplicate measurements the binning exists to prevent. Left unset
    # it falls back to the first fix, which is fine for a single drive and
    # wrong for a campaign -- so the app supplies one.
    grid_anchor_latitude: float | None = None
    grid_anchor_longitude: float | None = None
    # A bin with fewer windows than this is not written. One window is a
    # single 14 m slice: enough to see a signal, not enough to average
    # fading out of its level, and the level is what the solver reads as
    # distance.
    min_windows_per_bin: int = 3
    # A window can only be placed if the fix that tags it is recent. A stale
    # fix would put the measurement where the receiver used to be, which is
    # the one error that corrupts a campaign without looking wrong.
    max_position_age_seconds: float = 5.0
    # FFT frames analysed per window. Not every frame: a 65536-point FFT
    # over a full second at 5 MS/s is about 150 transforms, which a Pi
    # cannot finish inside the second it took to record. Spreading a bounded
    # number across the window keeps the analysis real-time and matches the
    # segmented/strided discipline the offline stages already use.
    frames_per_window: int = 24
    fft_size: int = 65_536
    overlap_ratio: float = 0.5
    chunk_frames: int = 262_144

    def validate(self) -> None:
        if self.window_seconds <= 0:
            raise ValueError("window_seconds must be positive")
        if self.bin_size_m <= 0:
            raise ValueError("bin_size_m must be positive")
        if self.min_windows_per_bin < 1:
            raise ValueError("min_windows_per_bin must be at least 1")
        if self.frames_per_window < 1:
            raise ValueError("frames_per_window must be at least 1")
        if self.sample_rate_hz <= 0 or self.center_frequency_hz <= 0:
            raise ValueError("sample rate and centre frequency must be positive")

    def to_device_settings(self) -> DeviceSettings:
        return DeviceSettings(
            driver=self.driver,
            sample_rate_hz=self.sample_rate_hz,
            center_frequency_hz=self.center_frequency_hz,
            if_gain_reduction_db=self.if_gain_reduction_db,
            lna_state=self.lna_state,
            agc=False,
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class LiveStats:
    windows_recorded: int = 0
    windows_without_position: int = 0
    windows_revisited: int = 0
    bins_written: int = 0
    bins_too_short: int = 0
    observations_written: int = 0
    overflow_count: int = 0
    # True when no campaign anchor was configured and the grid fell back to
    # the drive's own first fix. Reported so a campaign does not silently
    # end up with one coordinate frame per drive.
    anchor_from_first_fix: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _window_frame_starts(window_frames: int, settings: LiveSettings) -> list[int]:
    """Where in a window to take FFT frames, spread across its whole span.

    Spread rather than consecutive: the frames are meant to represent the
    second they came from, and clustering them at the start would measure
    the first fifth of the bin's travel and call it the bin.
    """
    available = fft_frame_count(window_frames, settings.fft_size, settings.overlap_ratio)
    if available < 1:
        return []
    wanted = min(settings.frames_per_window, available)
    span = window_frames - settings.fft_size
    if wanted == 1 or span <= 0:
        return [0]
    # Clamped to `span`: rounding to a whole frame step can land past the end
    # of the buffer, and a short final slice would reach the periodogram as a
    # length mismatch rather than as anything meaningful.
    step = max(1, settings.fft_size // 2)
    starts = sorted(
        {min(span, round(index * span / (wanted - 1) / step) * step) for index in range(wanted)}
    )
    return starts


class LiveSession:
    """One drive. Streams, bins, and writes virtual stops as it goes."""

    def __init__(
        self,
        *,
        session_id: str,
        settings: LiveSettings,
        band: BandProfile,
        site: SiteProfile,
        database_path: str | Path,
        position_provider: Callable[[], Position | None],
        device: IqDevice | None = None,
        on_bin: Callable[[dict[str, Any]], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        settings.validate()
        self.session_id = session_id
        self.settings = settings
        self.band = band
        self.site = site
        self.database_path = Path(database_path)
        self.position_provider = position_provider
        self.device = device
        self.on_bin = on_bin
        self.clock = clock
        self.stats = LiveStats()
        self._grid: BinGrid | None = None
        self._visit: BinVisit | None = None
        self._spectrum_settings = SpectrumSettings(
            fft_size=settings.fft_size, overlap_ratio=settings.overlap_ratio
        )

    # -- the loop ----------------------------------------------------------

    def run(self, *, stop: Callable[[], bool], connection: Any) -> LiveStats:
        """Stream until `stop()` says otherwise, writing bins as they close.

        `connection` is an open geo/survey database. Bins are written as they
        complete rather than at the end, so a drive that is interrupted --
        or simply still going -- has already contributed everything it
        measured up to that moment.
        """
        # Every survey run references a site row, and the gain recorded on
        # it is what later tells the campaign whether these levels are on
        # the same scale as a stationary stop's. Registered before the first
        # bin can reference it.
        upsert_site(
            connection,
            replace(
                self.site,
                gain=self.settings.if_gain_reduction_db,
                gain_mode="manual",
                lna_state=self.settings.lna_state,
            ),
        )
        resolved_device = self.device or SoapyIqDevice()
        resolved_device.open(self.settings.to_device_settings())
        window_frames = max(
            self.settings.fft_size,
            round(self.settings.window_seconds * self.settings.sample_rate_hz),
        )
        # One preallocated window, reused. Memory is fixed by the window
        # length, never by how long the drive lasts.
        buffer = np.empty(window_frames, dtype=np.complex64)
        filled = 0
        try:
            while not stop():
                chunk = np.asarray(
                    resolved_device.read_stream_chunk(
                        min(self.settings.chunk_frames, window_frames - filled)
                    )
                )
                if chunk.size:
                    take = min(chunk.size, window_frames - filled)
                    buffer[filled : filled + take] = chunk[:take]
                    filled += take
                if filled < window_frames:
                    continue
                self._consume_window(buffer, connection)
                filled = 0
        finally:
            self.stats.overflow_count = int(getattr(resolved_device, "overflow_count", 0))
            self._close_visit(connection)
            resolved_device.close()
        return self.stats

    def _consume_window(self, samples: np.ndarray, connection: Any) -> None:
        position = self.position_provider()
        if position is None or (
            self.clock() - position.at > self.settings.max_position_age_seconds
        ):
            # Recorded, not guessed at. A window with no fresh fix cannot be
            # placed, and placing it at the last known position is exactly
            # how a drive would smear measurements onto a spot it had left.
            self.stats.windows_without_position += 1
            return

        if self._grid is None:
            anchor_lat = self.settings.grid_anchor_latitude
            anchor_lon = self.settings.grid_anchor_longitude
            if anchor_lat is None or anchor_lon is None:
                anchor_lat, anchor_lon = position.latitude, position.longitude
                self.stats.anchor_from_first_fix = True
            self._grid = BinGrid(
                anchor_lat, anchor_lon, bin_size_m=self.settings.bin_size_m
            )
        key = self._grid.key_for(position.latitude, position.longitude)

        if self._visit is not None and self._visit.key != key:
            self._close_visit(connection)
        if self._grid.already_measured(key):
            self._grid.note_revisit()
            self.stats.windows_revisited = self._grid.revisited_windows
            return
        if self._visit is None:
            self._visit = BinVisit(key=key, started_utc=datetime.now(UTC).isoformat())

        spectrum = self._analyse(samples)
        if spectrum is None:
            return
        self._visit.spectra.append(spectrum)
        self._visit.latitudes.append(position.latitude)
        self._visit.longitudes.append(position.longitude)
        self.stats.windows_recorded += 1

    def _analyse(self, samples: np.ndarray) -> Any:
        starts = _window_frame_starts(samples.size, self.settings)
        if not starts:
            return None

        def frames() -> Iterator[np.ndarray]:
            for start in starts:
                yield samples[start : start + self.settings.fft_size]

        nyquist = self.settings.sample_rate_hz / 2.0
        return accumulate_segment_spectrum(
            frames(),
            fft_count=len(starts),
            sample_rate_hz=self.settings.sample_rate_hz,
            center_frequency_hz=self.settings.center_frequency_hz,
            nominal_low_hz=self.settings.center_frequency_hz - nyquist,
            nominal_high_hz=self.settings.center_frequency_hz + nyquist,
            settings=self._spectrum_settings,
        )

    # -- writing a bin -----------------------------------------------------

    def _close_visit(self, connection: Any) -> None:
        visit, self._visit = self._visit, None
        if visit is None or self._grid is None:
            return
        if visit.window_count < self.settings.min_windows_per_bin:
            # Passed through too fast to average fading out of the level.
            # Not marked measured: a later, slower pass through the same bin
            # should still get its chance.
            self.stats.bins_too_short += 1
            return

        detection = observations_from_segments(
            visit.spectra,
            band_profile=self.band,
            center_frequency_hz=self.settings.center_frequency_hz,
            sample_rate_hz=self.settings.sample_rate_hz,
            spectrum_settings=self._spectrum_settings,
        )
        passband = detection["usable_passband"]
        latitude, longitude = visit.centroid()
        analysed = (
            visit.window_count
            * len(_window_frame_starts(
                round(self.settings.window_seconds * self.settings.sample_rate_hz), self.settings
            ))
            * self.settings.fft_size
            / self.settings.sample_rate_hz
        )
        record = SurveyRunRecord(
            survey_run_id=visit.key.run_id(self._grid.tag),
            site_id=self.site.site_id,
            band_profile=self.band.name,
            # No file: this measurement never existed as a recording, and
            # saying so is more honest than naming a path that is not there.
            source_path=f"live://{self.session_id}",
            source_sha256=None,
            center_frequency_hz=self.settings.center_frequency_hz,
            sample_rate_hz=self.settings.sample_rate_hz,
            capture_start_utc=visit.started_utc,
            capture_time_source="live",
            requested_start_hz=self.band.start_frequency_hz,
            requested_stop_hz=self.band.stop_frequency_hz,
            usable_low_hz=passband.usable_low_hz,
            usable_high_hz=passband.usable_high_hz,
            coverage_status=passband.coverage_status,
            duration_seconds=visit.window_count * self.settings.window_seconds,
            analyzed_seconds=analysed,
            segment_count=visit.window_count,
            occupancy_threshold_db=detection["occupancy_threshold_db"],
            detection_settings=detection["detection_settings"].to_dict(),
            tool_version=__version__,
            settings={
                "mode": "live",
                "session_id": self.session_id,
                "grid_anchor": list(self._grid.anchor),
                "bin_x": visit.key.x,
                "bin_y": visit.key.y,
                "bin_size_m": self.settings.bin_size_m,
                "window_seconds": self.settings.window_seconds,
                "position_spread_m": round(visit.spread_m(), 1),
            },
            gps_latitude=latitude,
            gps_longitude=longitude,
            gps_source="live_gps",
        )
        import_survey_run(
            connection,
            run=record,
            observations=detection["observations"],
            raster_tolerance_hz=self.band.comparison.frequency_tolerance_hz,
        )
        self._grid.mark_measured(visit.key)
        self.stats.bins_written += 1
        self.stats.observations_written += len(detection["observations"])
        if self.on_bin is not None:
            self.on_bin(
                {
                    "survey_run_id": record.survey_run_id,
                    "latitude": latitude,
                    "longitude": longitude,
                    "windows": visit.window_count,
                    "observations": len(detection["observations"]),
                    "spread_m": round(visit.spread_m(), 1),
                    "bins_written": self.stats.bins_written,
                }
            )


def bin_size_for_speed(speed_kmh: float, window_seconds: float = DEFAULT_WINDOW_SECONDS) -> float:
    """Smallest bin that still holds several windows at this speed.

    A bin has to be crossed slowly enough to collect `min_windows_per_bin`
    windows, or every bin is discarded as too short. At 50 km/h a window
    covers 14 m, so three of them need 42 m of road -- comfortably inside a
    150 m bin. At 100 km/h it is 84 m, and the default starts to bite.
    """
    metres_per_window = speed_kmh * 1000.0 / 3600.0 * window_seconds
    return max(DEFAULT_BIN_SIZE_M, math.ceil(metres_per_window * 3 / 50.0) * 50.0)


__all__ = [
    "DEFAULT_WINDOW_SECONDS",
    "LiveSession",
    "LiveSettings",
    "LiveStats",
    "Position",
    "bin_size_for_speed",
]
