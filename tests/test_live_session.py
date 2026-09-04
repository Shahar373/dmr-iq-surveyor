"""A moving survey: binning, position discipline, and the drive-to-region path."""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pytest
from fixtures.geo_scenario import build_database, fast_solve_settings

from dmr_iq_surveyor.capture.device import DeviceSettings
from dmr_iq_surveyor.geo.model import LocalProjection, haversine_m
from dmr_iq_surveyor.geo.pipeline import materialise_measurements, solve_all_sites
from dmr_iq_surveyor.live.bins import BinGrid, BinKey, anchor_tag
from dmr_iq_surveyor.live.session import (
    LiveSession,
    LiveSettings,
    Position,
    bin_size_for_speed,
)
from dmr_iq_surveyor.survey.profiles import BandProfile, SiteProfile

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
