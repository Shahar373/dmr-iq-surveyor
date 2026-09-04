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
import queue
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

from dmr_iq_surveyor import __version__
from dmr_iq_surveyor.capture.device import DeviceSettings, IqDevice, SoapyIqDevice
from dmr_iq_surveyor.geo.model import haversine_m
from dmr_iq_surveyor.live.bins import (
    DEFAULT_BIN_SIZE_M,
    DEFAULT_LEDGER_CELL_M,
    MAX_ADAPTIVE_BIN_M,
    MIN_ADAPTIVE_BIN_M,
    BinGrid,
    BinKey,
    BinVisit,
    adaptive_bin_size_m,
)
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
    # Most windows one bin will accumulate before it is written and closed.
    # This is what makes standing still safe. A bin stays open until the
    # receiver leaves it, so a red light, a traffic jam or a parked car keeps
    # appending spectra to the same bin for as long as it sits there; each is
    # about 1.7 MB at a 65536-point FFT, so a minute of dwell holds ~100 MB
    # and a quarter of an hour holds over a gigabyte, and the Pi runs out of
    # memory while apparently doing nothing. Capping bounds that by the cap
    # alone, never by how long the drive or the dwell lasts. It also makes
    # dwelling cheap rather than merely survivable: once a bin is complete
    # the windows that follow it are dropped before the FFT, not after.
    max_windows_per_bin: int = 10
    # A window can only be placed if the fix that tags it is recent. A stale
    # fix would put the measurement where the receiver used to be, which is
    # the one error that corrupts a campaign without looking wrong.
    max_position_age_seconds: float = 5.0
    # How far the receiver may travel while one window fills. A window is a
    # fixed number of SAMPLES, not a fixed span of road: driver overflows
    # deliver nothing while the clock and the car keep going, so a window
    # interrupted by them covers more ground than a clean one at the same
    # speed. Past this the samples are an average over a stretch too long to
    # attribute to one place, and the window is dropped rather than smeared
    # across it. `None` derives it from the bin size, which is the length
    # scale the whole aggregation is built on.
    max_window_travel_m: float | None = None
    # Let the measurement span follow the speed instead of being one fixed
    # size. A 150 m bin is a compromise between a town, where it is most of a
    # street and the drive could afford twice the detail, and an open road,
    # where a car crosses it in five windows. With this on, the span is the
    # road it takes to gather `max_windows_per_bin` windows -- so a
    # measurement always averages the same amount of signal -- clamped to
    # [min_bin_size_m, max_bin_size_m] because the fading physics does not
    # care how fast the car is going.
    #
    # `bin_size_m` then stops being the grid: the grid becomes a fixed ledger
    # of `ledger_cell_m` squares recording which road has been measured, and a
    # measurement claims every cell its windows fell in. That is what keeps
    # variable spans from overlapping -- two measurements can never cover the
    # same ground, at any speed, on any day.
    adaptive_bin_size: bool = False
    ledger_cell_m: float = DEFAULT_LEDGER_CELL_M
    min_bin_size_m: float = MIN_ADAPTIVE_BIN_M
    max_bin_size_m: float = MAX_ADAPTIVE_BIN_M

    def travel_limit_m(self) -> float:
        if self.max_window_travel_m is not None:
            return float(self.max_window_travel_m)
        # Half the smallest measurement the configuration can produce: a
        # window that covered more than that cannot be attributed to one
        # place whichever span the speed happens to have chosen.
        smallest = self.min_bin_size_m if self.adaptive_bin_size else self.bin_size_m
        return smallest / 2.0

    def grid_cell_m(self) -> float:
        """The square the ledger is kept in -- fine when spans vary."""
        return self.ledger_cell_m if self.adaptive_bin_size else self.bin_size_m
    # FFT frames analysed per window. Not every frame: a 65536-point FFT
    # over a full second at 5 MS/s is about 150 transforms, which a Pi
    # cannot finish inside the second it took to record. Spreading a bounded
    # number across the window keeps the analysis real-time and matches the
    # segmented/strided discipline the offline stages already use.
    frames_per_window: int = 24
    # 16384 bins is 305 Hz at 5 MS/s -- forty bins across a 12.5 kHz channel,
    # which is what the detector integrates over. Measured against 65536 on a
    # synthesised 12.5 kHz carrier at 35, 20 and 10 dB, the reported channel
    # SNR agrees to within 0.2 dB, so a bin measured here and a stationary
    # stop measured offline at 65536 are on the SAME scale -- which they must
    # be, or the campaign acquires exactly the common-mode offset this project
    # checks for. What it buys is four times less memory per window (about
    # 0.43 MB) and roughly a third of the detection cost, both of which bind
    # on a Pi. 8192 was measured too and loses the weakest signal outright.
    fft_size: int = 16_384
    overlap_ratio: float = 0.5
    chunk_frames: int = 262_144
    # Analyse a finished bin on a second thread instead of in the streaming
    # loop. The detector scans every raster step of the band once PER WINDOW
    # in the bin -- about 0.11 s per window here, several times that on a Pi
    # -- and every second of that is a second the SDR is not being read. Off
    # by default only for tests that want one deterministic thread.
    background_analysis: bool = True

    def validate(self) -> None:
        if self.window_seconds <= 0:
            raise ValueError("window_seconds must be positive")
        if self.bin_size_m <= 0:
            raise ValueError("bin_size_m must be positive")
        if self.adaptive_bin_size:
            if self.ledger_cell_m <= 0:
                raise ValueError("ledger_cell_m must be positive")
            if not 0 < self.min_bin_size_m <= self.max_bin_size_m:
                raise ValueError("min_bin_size_m must be positive and at most max_bin_size_m")
            if self.ledger_cell_m > self.min_bin_size_m:
                # Otherwise the smallest measurement would not even fill one
                # ledger cell, and the ledger could not tell two of them apart.
                raise ValueError("ledger_cell_m must not exceed min_bin_size_m")
        if self.min_windows_per_bin < 1:
            raise ValueError("min_windows_per_bin must be at least 1")
        if self.max_windows_per_bin < self.min_windows_per_bin:
            raise ValueError("max_windows_per_bin must be at least min_windows_per_bin")
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
    # Windows dropped because the receiver covered too much ground while
    # they filled -- at speed, or because overflows stretched them.
    windows_too_fast: int = 0
    windows_revisited: int = 0
    # Windows dropped because the receiver was still sitting in a bin that
    # had already reached its window cap. Counted apart from revisits: one
    # says the drive retraced its route, the other says it stopped moving,
    # and an operator reading the summary needs to tell those apart.
    windows_dwelled: int = 0
    bins_written: int = 0
    # Of those, the ones closed by reaching the cap rather than by the
    # receiver leaving them.
    bins_capped: int = 0
    bins_too_short: int = 0
    # Bins whose detection ran in the streaming loop because the analysis
    # thread was still busy with the previous one. Not an error -- the
    # measurement is identical -- but it stalls the stream, so it is counted:
    # a drive full of these is a drive that wants a coarser --fft-size.
    bins_analysed_inline: int = 0
    # Bins lost because their analysis raised. Counted rather than allowed to
    # end the drive: the road is being driven now and cannot be re-driven.
    bins_failed: int = 0
    last_error: str = ""
    # The span the speed last asked for, and the speed it was asked at, so an
    # operator can see the adaptation working rather than infer it.
    bin_size_m: float = 0.0
    speed_kmh: float = 0.0
    # Windows dropped to keep consecutive measurements at least one span
    # apart. Crawling in traffic a bin fills its window cap long before it has
    # covered its span, and without this the next measurement would begin
    # metres from the last one -- inside the distance over which shadow fading
    # is still correlated, which is the one thing the binning exists to stop.
    windows_held_apart: int = 0
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
        # The bin the receiver is currently sitting in after it filled up,
        # so a dwell is not reported as if the route had been driven twice.
        self._dwell_key: BinKey | None = None
        self._spectrum_settings = SpectrumSettings(
            fft_size=settings.fft_size, overlap_ratio=settings.overlap_ratio
        )
        # Finished bins waiting to be analysed, and analysed bins waiting to
        # be written. Depth one on the way in: a deeper queue would hold more
        # spectra than the Pi can spare, and the back-pressure it creates is
        # handled by analysing inline rather than by dropping a measurement.
        # Smoothed ground speed, from the positions themselves rather than
        # from whatever the phone reports: `coords.speed` is often null, and
        # a single window's estimate jumps around with GPS noise.
        self._speed_ms: float | None = None
        # Where the last measurement began and how much road it was entitled
        # to. Until the receiver has covered that, no new measurement starts.
        self._hold_origin: tuple[float, float] | None = None
        self._hold_span_m: float = 0.0
        self._to_analyse: queue.Queue[BinVisit | None] | None = None
        self._analysed: queue.Queue[tuple[BinVisit, Any] | None] = queue.Queue()

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
        started_at: Position | None = None
        worker: threading.Thread | None = None
        if self.settings.background_analysis:
            self._to_analyse = queue.Queue(maxsize=1)
            worker = threading.Thread(
                target=self._analyse_finished_bins, name="live-detect", daemon=True
            )
            worker.start()
        try:
            while not stop():
                if filled == 0:
                    # Where the window began. Its measurement belongs midway
                    # between here and where it ends, not at either end.
                    started_at = self.position_provider()
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
                self._consume_window(buffer, started_at, connection)
                # Whatever the analysis thread finished while this window was
                # filling. Writing here, on the streaming thread, keeps the
                # database connection in the one thread that opened it.
                self._write_analysed(connection)
                filled = 0
                started_at = None
        finally:
            self.stats.overflow_count = int(getattr(resolved_device, "overflow_count", 0))
            self._close_visit(connection)
            # The radio is released before the last bins are analysed: holding
            # it through several seconds of arithmetic would keep it from the
            # next drive, or from another tool, for no gain.
            resolved_device.close()
            if worker is not None and self._to_analyse is not None:
                self._to_analyse.put(None)
                self._write_analysed(connection, until_finished=True)
                worker.join(timeout=60.0)
                self._to_analyse = None
        return self.stats

    def _consume_window(
        self, samples: np.ndarray, started_at: Position | None, connection: Any
    ) -> None:
        ended_at = self.position_provider()
        if (
            ended_at is None
            or started_at is None
            or self.clock() - ended_at.at > self.settings.max_position_age_seconds
        ):
            # Recorded, not guessed at. A window with no fresh fix cannot be
            # placed, and placing it at the last known position is exactly
            # how a drive would smear measurements onto a spot it had left.
            self.stats.windows_without_position += 1
            return

        travelled = haversine_m(
            started_at.latitude, started_at.longitude, ended_at.latitude, ended_at.longitude
        )
        if travelled > self.settings.travel_limit_m():
            self.stats.windows_too_fast += 1
            return

        # The midpoint, because the samples were gathered across the whole
        # span. Either endpoint would place the average where only half of
        # it was measured.
        position = Position(
            latitude=(started_at.latitude + ended_at.latitude) / 2.0,
            longitude=(started_at.longitude + ended_at.longitude) / 2.0,
            at=ended_at.at,
        )

        if self._grid is None:
            anchor_lat = self.settings.grid_anchor_latitude
            anchor_lon = self.settings.grid_anchor_longitude
            if anchor_lat is None or anchor_lon is None:
                anchor_lat, anchor_lon = position.latitude, position.longitude
                self.stats.anchor_from_first_fix = True
            self._grid = BinGrid(
                anchor_lat, anchor_lon, bin_size_m=self.settings.grid_cell_m()
            )
        self._note_speed(travelled, ended_at.at - started_at.at)
        key = self._grid.key_for(position.latitude, position.longitude)

        # Fixed spans close when the receiver leaves the square. Adaptive ones
        # close on distance travelled instead, so the square is only a ledger
        # of measured road and a measurement may span several of them.
        if self._visit is not None and not self.settings.adaptive_bin_size:
            if self._visit.key != key:
                self._close_visit(connection)
        if key != self._dwell_key:
            self._dwell_key = None
        if self._grid.already_measured(key):
            # Measured road. Whatever is open ends here rather than reaching
            # across it, so a measurement never straddles ground that already
            # has one.
            self._close_visit(connection)
            if self._dwell_key is not None:
                self.stats.windows_dwelled += 1
            else:
                self._grid.note_revisit()
                self.stats.windows_revisited = self._grid.revisited_windows
            # Returned before `_analyse`, so a stationary receiver costs no
            # transforms at all once its bin is complete.
            return
        if self._hold_origin is not None:
            # Fresh road, but too close to where the last measurement began.
            # This is what a window cap reached in slow traffic would otherwise
            # cost: ten windows can fill a bin in 40 m at 15 km/h, and the next
            # measurement would start there -- twice inside one shadow-fading
            # correlation length, counted by the solver as two independent
            # constraints. Also returned before `_analyse`, so crawling costs
            # no transforms it cannot use.
            if (
                haversine_m(*self._hold_origin, position.latitude, position.longitude)
                < self._hold_span_m
            ):
                self.stats.windows_held_apart += 1
                return
            self._hold_origin = None
        if self._visit is None:
            self._visit = BinVisit(
                key=key,
                started_utc=datetime.now(UTC).isoformat(),
                origin=(position.latitude, position.longitude),
                span_target_m=self._span_target_m(),
            )

        spectrum = self._analyse(samples)
        if spectrum is None:
            return
        self._visit.spectra.append(spectrum)
        self._visit.latitudes.append(position.latitude)
        self._visit.longitudes.append(position.longitude)
        self._visit.cells.add(key)
        self.stats.windows_recorded += 1
        if self._visit.window_count >= self.settings.max_windows_per_bin:
            # Full. Closed here rather than when the receiver moves off, so
            # the spectra are released immediately and a stationary operator
            # sees the measurement land instead of waiting for a departure
            # that may not come for a quarter of an hour.
            self._dwell_key = key
            self.stats.bins_capped += 1
            self._close_visit(connection)
        elif (
            self.settings.adaptive_bin_size
            and self._visit.travelled_m() >= self._visit.span_target_m
        ):
            # Enough road for one measurement at this speed.
            self._close_visit(connection)

    def _note_speed(self, travelled_m: float, seconds: float) -> None:
        # The interval between a window's first and last fix is supposed to be
        # one window. Much shorter than that and the fixes are not what they
        # claim -- two readings stamped at the same moment, a clock that
        # jumped, a phone replaying a cached position -- and dividing by it
        # yields the speed of a car that does not exist. Skipped rather than
        # smoothed in: one absurd sample would choose the span for the bins
        # after it too.
        if seconds < self.settings.window_seconds * 0.5:
            return
        instant = travelled_m / seconds
        # Exponential smoothing over roughly five windows. A single window's
        # estimate swings with GPS noise, and the span it would choose would
        # swing with it -- producing measurements of wildly different lengths
        # along one steady stretch of road.
        self._speed_ms = (
            instant if self._speed_ms is None else 0.8 * self._speed_ms + 0.2 * instant
        )
        self.stats.speed_kmh = round(self._speed_ms * 3.6, 1)

    def _span_target_m(self) -> float:
        if not self.settings.adaptive_bin_size:
            self.stats.bin_size_m = self.settings.bin_size_m
            return self.settings.bin_size_m
        span = adaptive_bin_size_m(
            self._speed_ms or 0.0,
            window_seconds=self.settings.window_seconds,
            windows_per_bin=self.settings.max_windows_per_bin,
            minimum_m=self.settings.min_bin_size_m,
            maximum_m=self.settings.max_bin_size_m,
        )
        self.stats.bin_size_m = round(span, 1)
        return span

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
        # Claimed the moment the bin is finished, not when its row is written.
        # The receiver can re-enter it while the analysis is still queued, and
        # a second visit to a bin already being measured is exactly the
        # duplicate the grid exists to prevent. EVERY cell the windows fell in
        # is claimed, not just the one the id is built from: a measurement
        # spanning 200 m of road has measured all 200 m of it.
        self._grid.mark_all_measured(visit.cells or {visit.key})
        if self.settings.adaptive_bin_size and visit.origin is not None:
            # Set only for a measurement that was actually taken: a visit
            # dropped for being too short has claimed nothing and must not
            # keep the next one from trying again over the same road.
            self._hold_origin = visit.origin
            self._hold_span_m = visit.span_target_m

        if self._to_analyse is not None:
            try:
                self._to_analyse.put_nowait(visit)
                return
            except queue.Full:
                # The analysis is slower than the road. Doing it here costs a
                # stall, which the binning absorbs; queueing without bound
                # would cost memory a Pi does not have, and dropping the visit
                # would cost the measurement itself.
                self.stats.bins_analysed_inline += 1
        self._write_visit(connection, visit, self._detect(visit))

    def _detect(self, visit: BinVisit) -> dict[str, Any]:
        return observations_from_segments(
            visit.spectra,
            band_profile=self.band,
            center_frequency_hz=self.settings.center_frequency_hz,
            sample_rate_hz=self.settings.sample_rate_hz,
            spectrum_settings=self._spectrum_settings,
        )

    def _analyse_finished_bins(self) -> None:
        """Detect on finished bins while the stream keeps running.

        Only arithmetic happens here. The database connection stays in the
        thread that opened it, and every write is done by `_write_analysed`
        on the streaming thread.
        """
        assert self._to_analyse is not None
        while True:
            visit = self._to_analyse.get()
            if visit is None:
                self._analysed.put(None)
                return
            try:
                self._analysed.put((visit, self._detect(visit)))
            except Exception as exc:  # noqa: BLE001 - one bad bin is not the drive
                self._analysed.put((visit, exc))

    def _write_analysed(self, connection: Any, *, until_finished: bool = False) -> None:
        while True:
            try:
                item = (
                    self._analysed.get(timeout=120.0)
                    if until_finished
                    else self._analysed.get_nowait()
                )
            except queue.Empty:
                return
            if item is None:
                return
            visit, detection = item
            if isinstance(detection, Exception):
                # Counted and named rather than raised: the drive is happening
                # on a road that cannot be re-driven, and every other bin is
                # still good.
                self.stats.bins_failed += 1
                self.stats.last_error = f"{type(detection).__name__}: {detection}"
                continue
            self._write_visit(connection, visit, detection)

    def _write_visit(
        self, connection: Any, visit: BinVisit, detection: dict[str, Any]
    ) -> None:
        if self._grid is None:
            return
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
                "bin_size_m": round(visit.span_target_m or self.settings.bin_size_m, 1),
                "adaptive_bin_size": self.settings.adaptive_bin_size,
                "ledger_cell_m": self._grid.bin_size_m,
                "cells": len(visit.cells) or 1,
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
