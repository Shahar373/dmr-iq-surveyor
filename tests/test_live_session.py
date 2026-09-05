"""A moving survey: binning, position discipline, and the drive-to-region path."""

from __future__ import annotations

import math
import time
from pathlib import Path

import numpy as np
import pytest
from fixtures.geo_scenario import build_database, fast_solve_settings

from dmr_iq_surveyor.capture.device import DeviceSettings
from dmr_iq_surveyor.geo.model import LocalProjection, haversine_m
from dmr_iq_surveyor.geo.pipeline import materialise_measurements, solve_all_sites
from dmr_iq_surveyor.geo.store import connect_geo_database
from dmr_iq_surveyor.live.bins import (
    DEFAULT_BIN_SIZE_M,
    MAX_ADAPTIVE_BIN_M,
    MIN_ADAPTIVE_BIN_M,
    BinGrid,
    BinKey,
    adaptive_bin_size_m,
    anchor_tag,
)
from dmr_iq_surveyor.live.session import (
    LiveSession,
    LiveSettings,
    Position,
    bin_size_for_speed,
)
from dmr_iq_surveyor.survey.profiles import BandProfile, SiteProfile
from dmr_iq_surveyor.survey.store import SurveyRunRecord, import_survey_run, upsert_site

# Site 30 in the shared fixture registry sits on this control channel.
SITE_30_HZ = 867_762_500.0
TRANSMITTER = (32.0700, 34.8000)
RATE = 200_000.0
# Far enough off-centre that the site never touches the receiver's own
# DC/LO artifact, which the pipeline would rightly refuse as a measurement
# of the radio rather than of any transmitter.
CENTER = SITE_30_HZ - 70_000.0


def _band() -> BandProfile:
    return BandProfile(
        name="live_test",
        label="live",
        start_frequency_hz=CENTER - 90_000.0,
        stop_frequency_hz=CENTER + 90_000.0,
        raster_spacings_hz=[12500.0, 6250.0],
        detection_overrides={
            "scan_step_hz": 6250.0,
            "integration_width_hz": 12500.0,
            "min_p95_channel_snr_db": 9.0,
            "min_average_channel_snr_db": 4.0,
            "merge_tolerance_hz": 4000.0,
        },
        segment_seconds=1.0,
        segment_stride_seconds=1.0,
        max_segments=40,
    )


class _Drive:
    """A vehicle on a straight line, and the signal it hears as it goes.

    The device and the position provider share this object, so the level in
    the samples always matches where the receiver is -- which is what makes
    the end-to-end assertion about geolocation mean anything.
    """

    def __init__(
        self,
        *,
        start: tuple[float, float],
        east_step_m: float,
        seconds_per_step: float = 1.0,
        reference_level_db: float = 40.0,
        exponent: float = 3.0,
    ) -> None:
        self.projection = LocalProjection(*start)
        self.start = start
        # Metres covered per window, converted to a speed so the car moves
        # continuously: a window's start and end are different places, which
        # is what the travel guard and the midpoint rule are about.
        self.speed_ms = east_step_m / seconds_per_step
        self.seconds_per_step = seconds_per_step
        self.reference_level_db = reference_level_db
        self.exponent = exponent
        self.now = 0.0
        self.windows = 0
        self.opened_with: DeviceSettings | None = None
        self.closed = False
        self._phase = 0
        self._rng = np.random.default_rng(7)

    # -- position ------------------------------------------------------
    def position(self) -> Position:
        metres_per_degree = 111_320.0
        latitude, longitude = self.start
        longitude += (
            self.now * self.speed_ms / (metres_per_degree * math.cos(math.radians(latitude)))
        )
        return Position(latitude=latitude, longitude=longitude, at=self.now)

    def clock(self) -> float:
        return self.now

    # -- device --------------------------------------------------------
    def open(self, settings: DeviceSettings) -> None:
        settings.validate()
        self.opened_with = settings

    def read_stream_chunk(self, max_frames: int) -> np.ndarray:
        # Every read costs its chunk of wall clock, and the car keeps going.
        self.now += max_frames / RATE
        position = self.position()
        distance = max(haversine_m(position.latitude, position.longitude, *TRANSMITTER), 50.0)
        level_db = self.reference_level_db - 10.0 * self.exponent * math.log10(distance / 1000.0)
        amplitude = 10.0 ** (level_db / 20.0) * 1e-3
        index = np.arange(self._phase, self._phase + max_frames, dtype=np.float64)
        self._phase += max_frames
        tone = amplitude * np.exp(2j * np.pi * (SITE_30_HZ - CENTER) * index / RATE)
        noise = self._rng.normal(scale=0.02, size=max_frames) + 1j * self._rng.normal(
            scale=0.02, size=max_frames
        )
        return (tone + noise).astype(np.complex64)

    def close(self) -> None:
        self.closed = True


def _settings(**overrides) -> LiveSettings:
    base = {
        "center_frequency_hz": CENTER,
        "sample_rate_hz": RATE,
        "window_seconds": 1.0,
        "bin_size_m": 150.0,
        "min_windows_per_bin": 2,
        "fft_size": 4096,
        "frames_per_window": 8,
        "chunk_frames": 200_000,
        "if_gain_reduction_db": 26.0,
        "lna_state": 8,
        "grid_anchor_latitude": 32.0700,
        "grid_anchor_longitude": 34.8000,
    }
    base.update(overrides)
    return LiveSettings(**base)


def _run(drive: _Drive, connection, *, windows: int, settings: LiveSettings) -> object:
    """Drive `windows` windows, advancing one position step per window."""
    def stop() -> bool:
        return drive.now >= windows * drive.seconds_per_step

    session = LiveSession(
        session_id="drive",
        settings=settings,
        band=_band(),
        site=SiteProfile(site_id="mobile", label="mobile"),
        database_path=":memory:",
        position_provider=drive.position,
        device=drive,
        clock=drive.clock,
    )
    return session.run(stop=stop, connection=connection)


# ------------------------------------------------------------------ binning


def test_positions_fall_into_bins_of_the_configured_size() -> None:
    grid = BinGrid(32.07, 34.80, bin_size_m=150.0)
    metres_per_degree = 111_320.0 * math.cos(math.radians(32.07))
    first = grid.key_for(32.07, 34.80)
    # 100 m east: same bin. 300 m east: two bins away.
    assert grid.key_for(32.07, 34.80 + 100.0 / metres_per_degree) == first
    far = grid.key_for(32.07, 34.80 + 400.0 / metres_per_degree)
    assert far != first
    assert far.x - first.x == 2


def test_run_ids_are_keyed_by_place_so_a_second_drive_replaces_rather_than_adds() -> None:
    """Two drives down the same street must not become two constraints.

    The id carries the grid square, not the session, so re-measuring a bin
    overwrites it. It also carries the anchor, because an index means
    nothing without the origin it was measured from -- grids anchored
    differently produce visibly different ids instead of colliding.
    """
    key = BinKey(12, -3)
    assert key.run_id(anchor_tag(32.07, 34.80)) == key.run_id(anchor_tag(32.07, 34.80))
    assert key.run_id(anchor_tag(32.07, 34.80)) != key.run_id(anchor_tag(32.09, 34.88))
    assert key.run_id("abcd1234") != BinKey(13, -3).run_id("abcd1234")


def test_a_bin_is_measured_once_and_revisits_are_counted_not_merged() -> None:
    grid = BinGrid(32.07, 34.80, bin_size_m=150.0)
    key = BinKey(3, 4)
    assert not grid.already_measured(key)
    grid.mark_measured(key)
    assert grid.already_measured(key)
    grid.note_revisit()
    grid.note_revisit()
    assert grid.revisited_windows == 2
    assert grid.measured_count == 1


def test_bin_size_grows_when_a_window_covers_too_much_ground() -> None:
    """Three windows have to fit inside a bin or every bin is discarded."""
    assert bin_size_for_speed(50.0) == 150.0
    assert bin_size_for_speed(200.0) > 150.0


# ------------------------------------------------------------- the session


def test_a_drive_writes_one_virtual_stop_per_bin(tmp_path: Path) -> None:
    connection = build_database(tmp_path / "db.sqlite3")
    # 75 m per window, so two windows fill a 150 m bin exactly.
    drive = _Drive(start=(32.0700, 34.8100), east_step_m=75.0)
    stats = _run(drive, connection, windows=12, settings=_settings())

    assert drive.opened_with is not None
    assert drive.opened_with.lna_state == 8, "the campaign's gain must reach the device"
    assert drive.closed is True
    assert stats.windows_recorded > 0
    assert stats.bins_written >= 4, stats.to_dict()

    runs = connection.execute(
        "SELECT survey_run_id, gps_latitude, gps_longitude, segment_count, gps_source "
        "FROM survey_runs WHERE survey_run_id LIKE 'live_%' ORDER BY survey_run_id"
    ).fetchall()
    assert len(runs) == stats.bins_written
    assert all(row["gps_source"] == "live_gps" for row in runs)
    assert all(row["segment_count"] >= 2 for row in runs)

    # Consecutive bins are a bin apart on the ground, which is the whole
    # point: they are separated enough to be near-independent evidence.
    positions = [(row["gps_latitude"], row["gps_longitude"]) for row in runs]
    gaps = [
        haversine_m(*positions[i], *positions[i + 1]) for i in range(len(positions) - 1)
    ]
    assert min(gaps) > 100.0, f"bins landed on top of each other: {gaps}"
    connection.close()


def test_windows_without_a_fresh_fix_are_dropped_not_placed(tmp_path: Path) -> None:
    """Placing a window at a stale position puts the measurement where the
    receiver used to be -- the error that corrupts a campaign silently."""
    connection = build_database(tmp_path / "db.sqlite3")
    drive = _Drive(start=(32.0700, 34.8100), east_step_m=75.0)

    def stale_position() -> Position:
        position = drive.position()
        position.at = drive.now - 60.0  # a minute old
        return position

    def stop() -> bool:
        return drive.now >= 6 * drive.seconds_per_step

    session = LiveSession(
        session_id="stale",
        settings=_settings(),
        band=_band(),
        site=SiteProfile(site_id="mobile", label="mobile"),
        database_path=":memory:",
        position_provider=stale_position,
        device=drive,
        clock=drive.clock,
    )
    stats = session.run(stop=stop, connection=connection)
    assert stats.windows_recorded == 0
    assert stats.windows_without_position >= 5
    assert stats.bins_written == 0
    connection.close()


def test_a_bin_crossed_too_fast_is_not_written(tmp_path: Path) -> None:
    """One window cannot average fading out of a level, and the level is
    what the solver reads as distance."""
    connection = build_database(tmp_path / "db.sqlite3")
    # 74 m per window: inside the 75 m travel limit, so the windows are kept,
    # but only two of them land in a 150 m bin and three are required.
    drive = _Drive(start=(32.0700, 34.8100), east_step_m=74.0)
    stats = _run(drive, connection, windows=10, settings=_settings(min_windows_per_bin=3))
    assert stats.windows_recorded > 0, "the windows themselves are fine"
    assert stats.windows_too_fast == 0
    # Two windows per bin against a minimum of three: most bins are refused.
    # Alignment lets the occasional bin collect a third, which is why this
    # asserts the balance rather than an exact zero.
    assert stats.bins_too_short > stats.bins_written
    connection.close()


def test_a_drive_past_a_transmitter_solves_through_the_existing_pipeline(
    tmp_path: Path,
) -> None:
    """The claim the whole mode rests on: a drive is a sequence of stops, so
    nothing downstream needs to know a drive happened."""
    connection = build_database(tmp_path / "db.sqlite3")
    # Three legs on different bearings from the transmitter, which is what
    # makes the geometry solvable rather than one long smear.
    for start in ((32.0700, 34.7880), (32.0790, 34.7940), (32.0620, 34.8060)):
        _run(_Drive(start=start, east_step_m=60.0), connection, windows=20,
             settings=_settings())
    connection.close()

    database = tmp_path / "db.sqlite3"
    summary = materialise_measurements(database_path=database)["summary"]
    assert summary["detections"] > 0, summary

    report = solve_all_sites(database_path=database, settings=fast_solve_settings())
    solved = [row for row in report["solutions"] if row["mode_latitude"] is not None]
    assert solved, [
        (row["site_key"], row["status"], row["detection_count"]) for row in report["solutions"]
    ]
    heard = next(row for row in solved if row["site_key"] == "BEE00:37D:1:30")
    error_km = (
        haversine_m(heard["mode_latitude"], heard["mode_longitude"], *TRANSMITTER) / 1000.0
    )
    assert error_km < 6.0, f"drive-derived mode landed {error_km:.1f} km away"
    assert heard["detection_count"] >= 4, (
        "the whole point is many bins contributing, not one long measurement"
    )


def test_a_window_that_covered_too_much_ground_is_dropped(tmp_path: Path) -> None:
    """A window is a fixed number of SAMPLES, not a fixed span of road.

    Driver overflows deliver nothing while the clock and the car keep
    going, so a window they interrupt covers more ground than a clean one at
    the same speed. Past the limit the samples average a stretch too long to
    attribute to one place.
    """
    connection = build_database(tmp_path / "db.sqlite3")
    drive = _Drive(start=(32.0700, 34.8100), east_step_m=300.0)
    # 300 m per window against a 75 m limit (half of a 150 m bin).
    stats = _run(drive, connection, windows=8, settings=_settings())
    assert stats.windows_too_fast > 0
    assert stats.windows_recorded == 0
    assert stats.bins_written == 0
    connection.close()


def test_a_window_is_placed_at_its_midpoint_not_its_end(tmp_path: Path) -> None:
    """The samples were gathered across the whole span, so either endpoint
    would place the average where only half of it was measured."""
    connection = build_database(tmp_path / "db.sqlite3")
    drive = _Drive(start=(32.0700, 34.8100), east_step_m=60.0)
    _run(drive, connection, windows=10, settings=_settings())
    rows = connection.execute(
        "SELECT gps_longitude FROM survey_runs WHERE survey_run_id LIKE 'live_%' "
        "ORDER BY survey_run_id"
    ).fetchall()
    connection.close()
    assert rows
    # Every recorded longitude sits inside the span the drive covered, and
    # the first is not simply the first sampled position.
    longitudes = [row["gps_longitude"] for row in rows]
    assert min(longitudes) > 34.8100
    assert max(longitudes) < 34.8100 + 10 * 60.0 / (111_320.0 * math.cos(math.radians(32.07)))


class _GappyDrive(_Drive):
    """A drive whose device drops every Nth read, as an overflow does."""

    def __init__(self, *, overflow_every: int, **kwargs) -> None:
        super().__init__(**kwargs)
        self.overflow_every = overflow_every
        self.reads = 0
        self.overflow_count = 0

    def read_stream_chunk(self, max_frames: int) -> np.ndarray:
        self.reads += 1
        if self.reads % self.overflow_every == 0:
            # The clock and the car keep going; no samples arrive.
            self.now += max_frames / RATE
            self.overflow_count += 1
            return np.empty(0, dtype=np.complex64)
        return super().read_stream_chunk(max_frames)


def test_binning_absorbs_driver_overflows(tmp_path: Path) -> None:
    """Losing half the windows must not cost half the measurements.

    This is what binning buys beyond decorrelation. At survey speeds a
    150 m bin collects several windows, so dropping some still leaves
    enough in each to average -- the redundancy is in the aggregation, not
    in any one window.
    """

    def drive_once(name: str, overflow_every: int) -> tuple[int, int]:
        connection = build_database(tmp_path / f"{name}.sqlite3")
        drive = (
            _Drive(start=(32.0700, 34.8100), east_step_m=30.0)
            if overflow_every == 0
            else _GappyDrive(
                overflow_every=overflow_every, start=(32.0700, 34.8100), east_step_m=30.0
            )
        )
        stats = _run(drive, connection, windows=40, settings=_settings())
        connection.close()
        return stats.bins_written, getattr(drive, "overflow_count", 0)

    clean_bins, _ = drive_once("clean", 0)
    gappy_bins, overflows = drive_once("gappy", 2)

    assert clean_bins > 0
    assert overflows > 0, "the fixture must actually drop reads"
    assert gappy_bins >= clean_bins * 0.7, (
        f"half the reads dropped cost {clean_bins - gappy_bins} of {clean_bins} bins; "
        "the aggregation is supposed to absorb that"
    )


def test_live_settings_reject_impossible_configuration() -> None:
    with pytest.raises(ValueError, match="window_seconds"):
        LiveSettings(window_seconds=0.0).validate()
    with pytest.raises(ValueError, match="bin_size_m"):
        LiveSettings(bin_size_m=-1.0).validate()
    with pytest.raises(ValueError, match="frames_per_window"):
        LiveSettings(frames_per_window=0).validate()
    with pytest.raises(ValueError, match="max_windows_per_bin"):
        LiveSettings(min_windows_per_bin=6, max_windows_per_bin=3).validate()


# ------------------------------------------------------------------- dwelling


class _ThereAndBack(_Drive):
    """Drives out along the line and returns down it, at the same speed."""

    def __init__(self, *, turn_seconds: float, **kwargs) -> None:
        super().__init__(**kwargs)
        self.turn_seconds = turn_seconds

    def position(self) -> Position:
        elapsed = (
            self.now if self.now <= self.turn_seconds else 2 * self.turn_seconds - self.now
        )
        metres_per_degree = 111_320.0
        latitude, longitude = self.start
        longitude += elapsed * self.speed_ms / (
            metres_per_degree * math.cos(math.radians(latitude))
        )
        return Position(latitude=latitude, longitude=longitude, at=self.now)


def _session(drive: _Drive, settings: LiveSettings) -> LiveSession:
    return LiveSession(
        session_id="drive",
        settings=settings,
        band=_band(),
        site=SiteProfile(site_id="mobile", label="mobile"),
        database_path=":memory:",
        position_provider=drive.position,
        device=drive,
        clock=drive.clock,
    )


def test_standing_still_writes_its_bin_and_then_holds_nothing(tmp_path: Path) -> None:
    """A parked receiver must not accumulate spectra for as long as it sits.

    Without a cap the open bin grows by one spectrum per second for the whole
    dwell -- about 1.7 MB each at the field FFT size -- so a long red light
    or a parked car eventually exhausts the Pi's memory while the operator
    sees nothing happening. The cap has to bound what is held, write the
    measurement instead of waiting for a departure, and stop transforming.
    """
    database = tmp_path / "parked.sqlite3"
    connection = build_database(database)
    drive = _Drive(start=TRANSMITTER, east_step_m=0.0)
    settings = _settings(min_windows_per_bin=2, max_windows_per_bin=3)
    session = _session(drive, settings)

    transforms: list[int] = []
    analyse = session._analyse

    def counting(samples):
        held = 0 if session._visit is None else session._visit.window_count
        # The bound, asserted where it actually has to hold rather than
        # inferred from the totals afterwards.
        assert held < settings.max_windows_per_bin
        transforms.append(held)
        return analyse(samples)

    session._analyse = counting
    stats = session.run(stop=lambda: drive.now >= 12.0, connection=connection)
    connection.close()

    assert stats.bins_written == 1
    assert stats.bins_capped == 1
    assert stats.windows_recorded == 3
    assert len(transforms) == 3, "windows after the cap must be dropped before the FFT"
    assert stats.windows_dwelled >= 8
    assert stats.windows_revisited == 0, "sitting still is not driving the route twice"


def test_a_dwell_is_reported_apart_from_a_genuine_revisit(tmp_path: Path) -> None:
    """Both drop windows; they mean opposite things to the operator."""
    database = tmp_path / "back.sqlite3"
    connection = build_database(database)
    drive = _ThereAndBack(start=TRANSMITTER, east_step_m=60.0, turn_seconds=8.0)
    settings = _settings(min_windows_per_bin=2, max_windows_per_bin=10)
    stats = _session(drive, settings).run(stop=lambda: drive.now >= 16.0, connection=connection)
    connection.close()

    assert stats.bins_written >= 2
    assert stats.bins_capped == 0, "60 m per window never fills a 150 m bin to the cap"
    assert stats.windows_revisited > 0, "the return leg re-enters bins already measured"
    assert stats.windows_dwelled == 0, "the receiver never stopped moving"


# ------------------------------------------------------- background analysis


def _run_session(drive: _Drive, connection, *, windows: int, settings: LiveSettings):
    session = _session(drive, settings)
    session.run(stop=lambda: drive.now >= windows * drive.seconds_per_step, connection=connection)
    return session


def _written(database: Path) -> dict[str, int]:
    connection = connect_geo_database(database)
    try:
        return {
            row["survey_run_id"]: row["n"]
            for row in connection.execute(
                "SELECT r.survey_run_id, "
                "(SELECT COUNT(*) FROM rf_observations o "
                " WHERE o.survey_run_id = r.survey_run_id) AS n "
                "FROM survey_runs r WHERE r.survey_run_id LIKE 'live_%'"
            )
        }
    finally:
        connection.close()


def test_analysing_a_bin_off_the_streaming_thread_changes_nothing_it_writes(
    tmp_path: Path,
) -> None:
    """The detector scans the whole band once per window in a bin, and every
    second of that is a second the SDR is not being read. Moving it off the
    loop is only allowed if the measurements are identical."""
    results = {}
    for background in (False, True):
        database = tmp_path / f"{background}.sqlite3"
        connection = build_database(database)
        drive = _Drive(start=(32.0700, 34.8000), east_step_m=60.0)
        settings = _settings(min_windows_per_bin=2, background_analysis=background)
        session = _run_session(drive, connection, windows=14, settings=settings)
        connection.close()
        results[background] = (_written(database), session.stats.bins_written)

    assert results[True][1] > 0, "the drive must actually write something"
    assert results[True] == results[False], (
        "the same road, analysed on a second thread, must produce the same rows"
    )


def test_analysis_slower_than_the_road_stalls_rather_than_losing_a_bin(
    tmp_path: Path,
) -> None:
    """The queue is one deep on purpose. When it is full the only options are
    to stall, to grow without bound, or to throw a measurement away -- and a
    measurement of a place the car has already left cannot be retaken."""
    database = tmp_path / "slow.sqlite3"
    connection = build_database(database)
    drive = _Drive(start=(32.0700, 34.8000), east_step_m=60.0)
    session = _session(drive, _settings(min_windows_per_bin=2))
    detect = session._detect

    def slow(visit):
        time.sleep(0.25)
        return detect(visit)

    session._detect = slow
    stats = session.run(stop=lambda: drive.now >= 20.0, connection=connection)
    connection.close()

    assert stats.bins_analysed_inline > 0, "the fixture must actually saturate the queue"
    assert stats.bins_failed == 0
    # Nothing is lost to the back-pressure: every bin that closed was written.
    assert len(_written(database)) == stats.bins_written


def test_one_bin_whose_analysis_fails_does_not_end_the_drive(tmp_path: Path) -> None:
    """The road is being driven now and cannot be re-driven. A bug in one
    bin's detection must cost that bin and be named, not the whole session."""
    database = tmp_path / "boom.sqlite3"
    connection = build_database(database)
    drive = _Drive(start=(32.0700, 34.8000), east_step_m=60.0)
    session = _session(drive, _settings(min_windows_per_bin=2))
    detect = session._detect
    calls = {"n": 0}

    def sometimes(visit):
        calls["n"] += 1
        if calls["n"] == 2:
            raise ValueError("synthetic detector failure")
        return detect(visit)

    session._detect = sometimes
    stats = session.run(stop=lambda: drive.now >= 20.0, connection=connection)
    connection.close()

    assert stats.bins_failed == 1
    assert "synthetic detector failure" in stats.last_error
    assert stats.bins_written >= 2, "the bins either side of the failure must survive"
    assert len(_written(database)) == stats.bins_written


# --------------------------------------------------------- adaptive spans


def test_the_span_follows_the_speed_between_its_two_limits() -> None:
    """The road it takes to gather ten windows -- clamped, because the fading
    physics does not care how fast the car is going."""
    def span(kmh, low=50.0, high=400.0):
        return adaptive_bin_size_m(
            kmh / 3.6, window_seconds=1.0, windows_per_bin=10,
            minimum_m=low, maximum_m=high,
        )

    assert span(30.0) == pytest.approx(83.3, abs=0.5)
    assert span(50.0) == pytest.approx(138.9, abs=0.5)
    assert span(110.0) == pytest.approx(305.6, abs=0.5)
    assert span(30.0, low=100.0) == 100.0, "the floor holds: closer would not be independent"
    assert span(110.0, high=150.0) == 150.0, "the ceiling holds"
    assert span(0.0, low=100.0) == 100.0, "standing still asks for the smallest legal span"


def test_the_shipped_span_is_the_one_that_does_not_over_claim() -> None:
    """Shortening the span in a city looks free -- urban fading decorrelates
    over 10-50 m -- and is not. Under correlated fading, a 90% region built
    from 50 m measurements contained the truth 60% of the time, and from 80 m
    or 100 m measurements 80% of the time; only at 150 m did it contain it
    90% of the time. A region that over-claims is worse than a large one, so
    the span does not vary. See bins.py for the table."""
    assert MIN_ADAPTIVE_BIN_M == MAX_ADAPTIVE_BIN_M == DEFAULT_BIN_SIZE_M == 150.0


def test_an_adaptive_drive_keeps_one_span_at_every_speed(tmp_path: Path) -> None:
    """The span is fixed at the only length that does not over-claim, so what
    changes with speed is how many windows fall inside it -- not its size."""
    slow, fast = {}, {}
    for speed_ms, into in ((9.0, slow), (28.0, fast)):
        database = tmp_path / f"{speed_ms}.sqlite3"
        connection = build_database(database)
        drive = _Drive(start=(32.0700, 34.8000), east_step_m=speed_ms)
        settings = _settings(
            min_windows_per_bin=2,
            max_windows_per_bin=10,
            adaptive_bin_size=True,
            ledger_cell_m=50.0,
        )
        stats = _run(drive, connection, windows=40, settings=settings)
        connection.close()
        into["stats"] = stats
        into["spans"] = _spans(database)

    assert slow["spans"] and fast["spans"]
    assert set(slow["spans"]) == set(fast["spans"]) == {150.0}
    assert slow["stats"].bins_written < fast["stats"].bins_written, (
        "over the same number of windows the slower drive covers less road, so it must "
        "produce fewer measurements -- not shorter ones"
    )
    assert slow["stats"].windows_held_apart > 0, (
        "crawling, windows must be held back to keep measurements a full span apart"
    )
    assert fast["stats"].windows_held_apart == 0, "at speed nothing needs holding back"
    assert min(slow["spans"]) >= 150.0 and max(fast["spans"]) <= 150.0


def _spans(database: Path) -> list[float]:
    import json

    connection = connect_geo_database(database)
    try:
        return [
            json.loads(row["settings_json"] or "{}")["bin_size_m"]
            for row in connection.execute(
                "SELECT settings_json FROM survey_runs WHERE survey_run_id LIKE 'live_%'"
            )
        ]
    finally:
        connection.close()


def test_variable_spans_never_measure_the_same_road_twice(tmp_path: Path) -> None:
    """The whole reason the grid became a fine ledger. With spans that change
    with speed, two measurements of different lengths could otherwise overlap
    -- and two measurements of one place are the correlated evidence the
    binning exists to keep out of the posterior."""
    database = tmp_path / "there_and_back.sqlite3"
    connection = build_database(database)
    drive = _ThereAndBack(start=(32.0700, 34.8000), east_step_m=25.0, turn_seconds=20.0)
    settings = _settings(
        min_windows_per_bin=2, max_windows_per_bin=10, adaptive_bin_size=True
    )
    session = _session(drive, settings)
    stats = session.run(stop=lambda: drive.now >= 40.0, connection=connection)
    connection.close()

    assert stats.bins_written >= 2
    assert stats.windows_revisited > 0, "the return leg must actually re-enter measured road"
    # Every ledger cell is claimed by exactly one measurement: the grid's own
    # set is the proof, since a cell can only be added once.
    grid = session._grid
    assert grid is not None
    claimed = grid.measured_count
    assert claimed >= stats.bins_written
    positions = _positions(database)
    for i, (lat_a, lon_a) in enumerate(positions):
        for lat_b, lon_b in positions[i + 1 :]:
            assert haversine_m(lat_a, lon_a, lat_b, lon_b) > 40.0, (
                "two measurements landed on top of each other"
            )


def _positions(database: Path) -> list[tuple[float, float]]:
    connection = connect_geo_database(database)
    try:
        return [
            (row["gps_latitude"], row["gps_longitude"])
            for row in connection.execute(
                "SELECT gps_latitude, gps_longitude FROM survey_runs "
                "WHERE survey_run_id LIKE 'live_%'"
            )
        ]
    finally:
        connection.close()


def test_a_fixed_span_drive_is_unchanged_by_the_adaptive_code(tmp_path: Path) -> None:
    """Adaptive spans are opt-in. With them off, the grid is still the bin."""
    database = tmp_path / "fixed.sqlite3"
    connection = build_database(database)
    drive = _Drive(start=(32.0700, 34.8000), east_step_m=60.0)
    stats = _run(drive, connection, windows=14, settings=_settings(min_windows_per_bin=2))
    connection.close()
    assert stats.bins_written > 0
    assert set(_spans(database)) == {150.0}


def test_slow_traffic_does_not_pack_measurements_inside_one_fading_length(
    tmp_path: Path,
) -> None:
    """Crawling, a bin fills its window cap long before it has covered its
    span: ten one-second windows at 15 km/h is 42 m. Without a hold, the next
    measurement would start there -- two measurements inside one shadow-fading
    correlation length, which the solver would count as two independent
    constraints. That is the exact error the binning exists to prevent, and it
    only appears in traffic."""
    database = tmp_path / "traffic.sqlite3"
    connection = build_database(database)
    drive = _Drive(start=(32.0700, 34.8000), east_step_m=4.2)  # 15 km/h
    settings = _settings(
        min_windows_per_bin=2, max_windows_per_bin=10, adaptive_bin_size=True
    )
    stats = _run(drive, connection, windows=120, settings=settings)
    connection.close()

    assert stats.bins_written >= 3, "the drive must produce several measurements"
    assert stats.bins_capped >= 3, "the fixture must actually hit the cap before the span"
    assert stats.windows_held_apart > 0, "windows must be held back, not measured"
    positions = _positions(database)
    for i, (lat_a, lon_a) in enumerate(positions):
        for lat_b, lon_b in positions[i + 1 :]:
            gap = haversine_m(lat_a, lon_a, lat_b, lon_b)
            assert gap >= 80.0, (
                f"two measurements {gap:.0f} m apart at 15 km/h -- inside the distance "
                "over which urban shadow fading is still correlated"
            )


# ------------------------------------------------- a second day, same road


def _superseded(database: Path) -> dict[str, str]:
    connection = connect_geo_database(database)
    try:
        return {
            row["survey_run_id"]: row["reason"]
            for row in connection.execute("SELECT survey_run_id, reason FROM geo_run_exclusions")
        }
    finally:
        connection.close()


def test_a_second_drive_over_the_same_road_keeps_both_and_uses_the_newer(tmp_path: Path) -> None:
    """Day two must not silently overwrite day one, and must not count twice.

    The earlier bin stays in the database with its observations, barred from
    the solve by an exclusion that names its replacement. The solver sees one
    constraint per place -- the newest -- and the operator can put the two
    side by side to see whether the road measured the same on both days.
    """
    database = tmp_path / "two_days.sqlite3"
    settings = _settings(min_windows_per_bin=2)

    connection = build_database(database)
    drive_a = _Drive(start=(32.0700, 34.8000), east_step_m=60.0)
    session_a = LiveSession(
        session_id="day1", settings=settings, band=_band(),
        site=SiteProfile(site_id="mobile", label="mobile"), database_path=database,
        position_provider=drive_a.position, device=drive_a, clock=drive_a.clock,
    )
    stats_a = session_a.run(stop=lambda: drive_a.now >= 14.0, connection=connection)
    connection.close()
    assert stats_a.bins_written >= 2
    assert stats_a.bins_superseded == 0, "a first drive supersedes nothing"
    first_ids = set(_written(database))
    assert all(run_id.endswith("_day1") for run_id in first_ids)

    connection = build_database(database)
    drive_b = _Drive(start=(32.0700, 34.8000), east_step_m=60.0)
    session_b = LiveSession(
        session_id="day2", settings=settings, band=_band(),
        site=SiteProfile(site_id="mobile", label="mobile"), database_path=database,
        position_provider=drive_b.position, device=drive_b, clock=drive_b.clock,
    )
    stats_b = session_b.run(stop=lambda: drive_b.now >= 14.0, connection=connection)
    connection.close()

    all_ids = set(_written(database))
    second_ids = all_ids - first_ids
    assert first_ids <= all_ids, "day one's rows must still be there"
    assert second_ids and all(run_id.endswith("_day2") for run_id in second_ids)
    assert stats_b.bins_superseded == len(first_ids), (
        "every bin day two re-measured must be reported as superseded"
    )

    exclusions = _superseded(database)
    for run_id in first_ids:
        assert run_id in exclusions, f"{run_id} from day one must be barred from the solve"
        assert exclusions[run_id].startswith("superseded by live_")
        assert exclusions[run_id].endswith("_day2")
    for run_id in second_ids:
        assert run_id not in exclusions, "the newer measurement is the one that counts"

    # And the solve sees exactly one constraint per place: the newer one.
    summary = materialise_measurements(database_path=database)["summary"]
    connection = connect_geo_database(database)
    try:
        usable_runs = {
            row["survey_run_id"]
            for row in connection.execute(
                "SELECT DISTINCT survey_run_id FROM geo_measurements WHERE usability = 'usable'"
            )
        }
        barred_runs = {
            row["survey_run_id"]
            for row in connection.execute(
                "SELECT DISTINCT survey_run_id FROM geo_measurements "
                "WHERE usability = 'run_excluded'"
            )
        }
    finally:
        connection.close()
    assert usable_runs & first_ids == set(), "day one must contribute nothing"
    assert first_ids <= barred_runs
    assert usable_runs & second_ids == second_ids
    assert summary["usable"] > 0


def test_a_legacy_place_keyed_bin_is_superseded_too(tmp_path: Path) -> None:
    """Campaigns from before ids carried the session hold bare place keys.
    A drive over that road must supersede them as well, or the old bin would
    silently keep counting beside the new one."""
    database = tmp_path / "legacy.sqlite3"
    settings = _settings(min_windows_per_bin=2)
    connection = build_database(database)
    drive = _Drive(start=(32.0700, 34.8000), east_step_m=60.0)
    session = LiveSession(
        session_id="new", settings=settings, band=_band(),
        site=SiteProfile(site_id="mobile", label="mobile"), database_path=database,
        position_provider=drive.position, device=drive, clock=drive.clock,
    )
    # Plant a legacy row under the bare place key of the first bin the drive
    # will produce, exactly as an older build would have written it.
    grid = BinGrid(32.0700, 34.8000, bin_size_m=150.0)
    legacy_key = grid.key_for(32.0700, 34.8000 + 0.0003).run_id(grid.tag)
    # Written through the real record type, as the older build did, so the
    # row is exactly what a legacy campaign holds -- and so this test does
    # not have to be rewritten every time the schema gains a column.
    upsert_site(connection, SiteProfile(site_id="mobile", label="mobile"))
    import_survey_run(
        connection,
        run=SurveyRunRecord(
            survey_run_id=legacy_key,
            site_id="mobile",
            band_profile="live_test",
            source_path="live://old",
            source_sha256=None,
            center_frequency_hz=CENTER,
            sample_rate_hz=RATE,
            capture_start_utc="2026-01-01T00:00:00+00:00",
            capture_time_source="live",
            requested_start_hz=CENTER - 90_000.0,
            requested_stop_hz=CENTER + 90_000.0,
            usable_low_hz=CENTER - 90_000.0,
            usable_high_hz=CENTER + 90_000.0,
            coverage_status="complete",
            duration_seconds=3.0,
            analyzed_seconds=3.0,
            segment_count=3,
            occupancy_threshold_db=8.0,
            detection_settings={},
            tool_version="legacy",
            settings={"mode": "live", "session_id": "old"},
            gps_latitude=32.0700,
            gps_longitude=34.8003,
            gps_source="live_gps",
        ),
        observations=[],
        raster_tolerance_hz=6250.0,
    )

    stats = session.run(stop=lambda: drive.now >= 6.0, connection=connection)
    connection.close()
    assert stats.bins_written >= 1
    exclusions = _superseded(database)
    assert legacy_key in exclusions, "the bare-key legacy row must be superseded"
    assert exclusions[legacy_key].startswith("superseded by ")


# ------------------------------------------------------ a stop mid-drive


class _Parkable(_Drive):
    """A drive that can be told to stop moving, as a car that pulled over does."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.parked_at: float | None = None

    def position(self) -> Position:
        now = self.now if self.parked_at is None else min(self.now, self.parked_at)
        metres_per_degree = 111_320.0
        latitude, longitude = self.start
        longitude += now * self.speed_ms / (metres_per_degree * math.cos(math.radians(latitude)))
        return Position(latitude=latitude, longitude=longitude, at=self.now)


def _hold_after(drive: _Parkable, after_seconds: float, seconds: float, *, park: bool):
    """A hold provider that asks once, after the drive has been going a while."""
    asked = {"done": False}

    def provider() -> float | None:
        if asked["done"] or drive.now < after_seconds:
            return None
        asked["done"] = True
        if park:
            drive.parked_at = drive.now
        return seconds

    return provider


def _runs_by_mode(database: Path) -> dict[str, list[dict]]:
    import json

    connection = connect_geo_database(database)
    try:
        out: dict[str, list[dict]] = {}
        for row in connection.execute(
            "SELECT survey_run_id, settings_json, duration_seconds FROM survey_runs "
            "WHERE survey_run_id LIKE 'live_%'"
        ):
            settings = json.loads(row["settings_json"] or "{}")
            out.setdefault(settings.get("mode", "?"), []).append(
                {"id": row["survey_run_id"], "settings": settings, "duration": row["duration_seconds"]}
            )
        return out
    finally:
        connection.close()


def test_a_hold_writes_a_stationary_measurement_and_the_drive_carries_on(tmp_path: Path) -> None:
    """Pull over, measure, drive on. The hold is written as its own kind of
    run, it supersedes the drive bin taken at that spot, and bins resume
    afterwards -- a hold is a pause in the binning, not the end of it."""
    database = tmp_path / "hold.sqlite3"
    connection = build_database(database)
    drive = _Parkable(start=(32.0700, 34.8000), east_step_m=60.0)
    settings = _settings(min_windows_per_bin=2)
    session = LiveSession(
        session_id="s", settings=settings, band=_band(),
        site=SiteProfile(site_id="mobile", label="mobile"), database_path=database,
        position_provider=drive.position, device=drive, clock=drive.clock,
        hold_provider=_hold_after(drive, after_seconds=6.0, seconds=5.0, park=True),
    )

    def resume():
        # The car drives on once the hold is over.
        if drive.parked_at is not None and not session.stats.hold_active and drive.now > 12.0:
            drive.parked_at = None
        return drive.now >= 24.0

    stats = session.run(stop=resume, connection=connection)
    connection.close()

    assert stats.holds_written == 1
    assert stats.hold_seconds_total == 5.0
    assert not stats.hold_active
    runs = _runs_by_mode(database)
    holds = runs.get("live_stop", [])
    assert len(holds) == 1
    hold = holds[0]
    assert hold["id"].endswith("_s_hold")
    assert hold["settings"]["hold_seconds"] == 5.0
    assert hold["settings"]["moved_during_hold"] is False, "the car was parked"
    assert hold["duration"] >= 4.0, "the hold integrated for about its whole length"

    # The drive bin at that spot is superseded by the hold, whichever was
    # written first -- both orders are possible with the analysis thread.
    exclusions = _superseded(database)
    place = hold["settings"]["place_key"]
    drive_bin_here = [r for r in runs.get("live", []) if r["settings"].get("place_key") == place]
    for run in drive_bin_here:
        assert run["id"] in exclusions
        assert exclusions[run["id"]].endswith("_hold")
    assert hold["id"] not in exclusions, "the hold is the measurement of record here"

    # And the drive kept going: bins exist that were written after the hold.
    later = [r for r in runs.get("live", []) if r["id"] not in exclusions]
    assert later, "bins must resume after the hold"
    assert stats.bins_written >= 2


def test_a_hold_that_moved_says_so(tmp_path: Path) -> None:
    """A "stationary" measurement whose positions spread past a parked car's
    GPS jitter was not stationary. It is kept -- the data is real -- and the
    row says what happened, rather than being passed off as still."""
    database = tmp_path / "moved.sqlite3"
    connection = build_database(database)
    drive = _Parkable(start=(32.0700, 34.8000), east_step_m=60.0)
    session = LiveSession(
        session_id="m", settings=_settings(min_windows_per_bin=2), band=_band(),
        site=SiteProfile(site_id="mobile", label="mobile"), database_path=database,
        position_provider=drive.position, device=drive, clock=drive.clock,
        hold_provider=_hold_after(drive, after_seconds=4.0, seconds=4.0, park=False),
    )
    session.run(stop=lambda: drive.now >= 14.0, connection=connection)
    connection.close()
    holds = _runs_by_mode(database).get("live_stop", [])
    assert len(holds) == 1
    assert holds[0]["settings"]["moved_during_hold"] is True
    assert holds[0]["settings"]["position_spread_m"] > 30.0


def test_window_frame_starts_is_the_shared_spread(tmp_path) -> None:
    """The drive window and the stationary drive view take their frames from
    the same function, so the two statistics cannot drift apart."""
    from dmr_iq_surveyor.live.session import LiveSettings, _window_frame_starts
    from dmr_iq_surveyor.survey.discovery import spread_frame_starts

    settings = LiveSettings(fft_size=4096, frames_per_window=24)
    window = 200_000  # one second at the fixture rate
    starts = _window_frame_starts(window, settings)
    assert starts == spread_frame_starts(window, fft_size=4096, overlap_ratio=0.5, wanted=24)
    assert len(starts) == 24
    assert starts[0] == 0 and starts[-1] <= window - 4096
    assert all(b - a >= 2048 for a, b in zip(starts, starts[1:], strict=False))
    # Too short for one frame: nothing, not a crash and not a partial frame.
    assert spread_frame_starts(1000, fft_size=4096, overlap_ratio=0.5, wanted=24) == []
    assert spread_frame_starts(4096, fft_size=4096, overlap_ratio=0.5, wanted=24) == [0]
