from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import numpy as np
import pytest

from dmr_iq_surveyor.capture.core import (
    CaptureSettings,
    run_capture,
    run_capture_and_survey,
    sdrconnect_style_filename,
)
from dmr_iq_surveyor.capture.device import DeviceSettings
from dmr_iq_surveyor.iq.metadata import inspect_wave_iq
from dmr_iq_surveyor.iq.reader import IQMemmapReader
from dmr_iq_surveyor.survey.profiles import BandProfile, SiteProfile
from dmr_iq_surveyor.survey.store import connect_survey_database, get_run

SAMPLE_RATE_HZ = 200_000.0
CENTER_HZ = 868_000_000.0


class FakeIqDevice:
    """Deterministic `IqDevice` stub: yields a tone plus noise in caller-sized
    chunks, with no SoapySDR import and no hardware. Records the settings it
    was opened with so tests can assert AGC/gain/rate/frequency passthrough."""

    def __init__(
        self,
        *,
        tone_offset_hz: float = 0.0,
        amplitude: float = 0.3,
        noise_std: float = 0.01,
        seed: int = 0,
    ) -> None:
        self.opened_with: DeviceSettings | None = None
        self.closed = False
        self.read_calls: list[int] = []
        self._tone_offset_hz = tone_offset_hz
        self._amplitude = amplitude
        self._noise_std = noise_std
        self._rng = np.random.default_rng(seed)
        self._sample_rate_hz = 0.0
        self._samples_emitted = 0

    def open(self, settings: DeviceSettings) -> None:
        settings.validate()
        self.opened_with = settings
        self._sample_rate_hz = settings.sample_rate_hz

    def read_stream_chunk(self, max_frames: int) -> np.ndarray:
        self.read_calls.append(max_frames)
        n = max_frames
        t = (np.arange(n, dtype=np.float64) + self._samples_emitted) / self._sample_rate_hz
        carrier = self._amplitude * np.exp(1j * 2.0 * np.pi * self._tone_offset_hz * t)
        noise = self._rng.normal(scale=self._noise_std, size=n) + 1j * self._rng.normal(
            scale=self._noise_std, size=n
        )
        self._samples_emitted += n
        return (carrier + noise).astype(np.complex64)

    def close(self) -> None:
        self.closed = True


def _band_profile() -> BandProfile:
    return BandProfile(
        name="test_band",
        label="test",
        start_frequency_hz=867_800_000.0,
        stop_frequency_hz=868_200_000.0,
        raster_spacings_hz=[12500.0, 6250.0],
        detection_overrides={
            "scan_step_hz": 6250.0,
            "integration_width_hz": 12500.0,
            "min_p95_channel_snr_db": 9.0,
            "min_average_channel_snr_db": 4.0,
            "min_equivalent_width_hz": 1500.0,
            "min_width_90_hz": 1000.0,
            "max_width_90_hz": 13000.0,
            "merge_tolerance_hz": 4000.0,
            "passband_warning_low_hz": 866_000_000.0,
            "passband_warning_high_hz": 870_000_000.0,
        },
        segment_seconds=1.0,
        segment_stride_seconds=1.0,
        max_segments=6,
    )


def test_sdrconnect_style_filename_matches_discovery_fallback_pattern(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    from dmr_iq_surveyor.survey.discovery import resolve_capture_time

    name = sdrconnect_style_filename(868_000_000.0, moment=datetime(2026, 8, 15, 12, 0, 0, tzinfo=UTC))
    assert name == "SDRconnect_IQ_20260815_120000_868000000HZ.wav"

    # Round-trip through the same fallback survey/discovery.py uses when a
    # recording has no (or an unreadable) auxi chunk.
    device = FakeIqDevice()
    settings = CaptureSettings(
        center_frequency_hz=CENTER_HZ,
        sample_rate_hz=SAMPLE_RATE_HZ,
        duration_seconds=0.05,
        if_gain_reduction_db=20.0,
        write_auxi=False,
    )
    manifest = run_capture(tmp_path, settings=settings, device=device, filename=name)
    info = inspect_wave_iq(manifest["wav_path"])
    capture_time, source = resolve_capture_time(info)
    assert source == "filename"
    assert capture_time == "2026-08-15T12:00:00+00:00"


def test_capture_settings_rejects_agc_and_gain_together() -> None:
    settings = CaptureSettings(
        center_frequency_hz=CENTER_HZ,
        sample_rate_hz=SAMPLE_RATE_HZ,
        duration_seconds=1.0,
        agc=True,
        if_gain_reduction_db=10.0,
    )
    with pytest.raises(ValueError):
        settings.validate()


def test_capture_settings_requires_gain_when_agc_off() -> None:
    settings = CaptureSettings(
        center_frequency_hz=CENTER_HZ,
        sample_rate_hz=SAMPLE_RATE_HZ,
        duration_seconds=1.0,
        agc=False,
        if_gain_reduction_db=None,
    )
    with pytest.raises(ValueError):
        settings.validate()


def test_run_capture_writes_wav_that_round_trips(tmp_path: Path) -> None:
    device = FakeIqDevice(tone_offset_hz=20_000.0, amplitude=0.4)
    settings = CaptureSettings(
        center_frequency_hz=CENTER_HZ,
        sample_rate_hz=SAMPLE_RATE_HZ,
        duration_seconds=0.5,
        if_gain_reduction_db=30.0,
        agc=False,
        chunk_frames=8192,
    )
    manifest = run_capture(tmp_path, settings=settings, device=device)

    wav_path = Path(manifest["wav_path"])
    assert wav_path.is_file()
    assert wav_path.parent == tmp_path.resolve()
    assert wav_path.name.startswith("SDRconnect_IQ_")
    assert wav_path.name.endswith("_868000000HZ.wav")

    expected_frames = round(0.5 * SAMPLE_RATE_HZ)
    assert manifest["frame_count"] == expected_frames
    assert manifest["requested_frame_count"] == expected_frames
    assert manifest["actual_duration_seconds"] == pytest.approx(0.5, abs=1e-6)
    assert manifest["peak_rss_bytes"] > 0

    # AGC-off / gain / rate / frequency passthrough to the (mocked) device.
    assert device.closed is True
    assert device.opened_with is not None
    assert device.opened_with.agc is False
    assert device.opened_with.if_gain_reduction_db == 30.0
    assert device.opened_with.sample_rate_hz == SAMPLE_RATE_HZ
    assert device.opened_with.center_frequency_hz == CENTER_HZ
    assert device.opened_with.driver == "sdrplay"

    report_path = tmp_path / f"{wav_path.stem}_capture_report.json"
    assert report_path.is_file()
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["frame_count"] == expected_frames
    assert report["settings"]["if_gain_reduction_db"] == 30.0
    assert report["settings"]["agc"] is False

    info = inspect_wave_iq(wav_path)
    assert info.frame_count == expected_frames
    assert info.fmt.sample_rate_hz == SAMPLE_RATE_HZ
    assert info.center_frequency_hz == CENTER_HZ
    assert info.center_frequency_source == "auxi"
    assert info.auxi is not None
    assert info.auxi.center_frequency_hz == CENTER_HZ

    reader = IQMemmapReader(info)
    samples = reader.read_complex(0, info.frame_count)
    assert samples.shape[0] == expected_frames


def test_run_capture_passes_agc_through_to_device(tmp_path: Path) -> None:
    device = FakeIqDevice()
    settings = CaptureSettings(
        center_frequency_hz=CENTER_HZ,
        sample_rate_hz=SAMPLE_RATE_HZ,
        duration_seconds=0.1,
        agc=True,
        if_gain_reduction_db=None,
    )
    run_capture(tmp_path, settings=settings, device=device)
    assert device.opened_with is not None
    assert device.opened_with.agc is True
    assert device.opened_with.if_gain_reduction_db is None


def test_run_capture_trims_overshoot_to_requested_duration(tmp_path: Path) -> None:
    """The fake device's chunk size is larger than the remaining frame
    budget for the final read; run_capture must trim to exactly the
    requested duration rather than overshoot it."""
    device = FakeIqDevice()
    settings = CaptureSettings(
        center_frequency_hz=CENTER_HZ,
        sample_rate_hz=SAMPLE_RATE_HZ,
        duration_seconds=0.05,
        if_gain_reduction_db=20.0,
        chunk_frames=100_000,  # far larger than the ~10k frames requested
    )
    manifest = run_capture(tmp_path, settings=settings, device=device)
    expected_frames = round(0.05 * SAMPLE_RATE_HZ)
    assert manifest["frame_count"] == expected_frames
    info = inspect_wave_iq(manifest["wav_path"])
    assert info.frame_count == expected_frames


def test_run_capture_and_survey_detects_injected_tone(tmp_path: Path) -> None:
    """The full 'one command' path: capture via a fake device, then
    immediately run the existing, unmodified survey pipeline on the result."""
    device = FakeIqDevice(tone_offset_hz=50_000.0, amplitude=0.5, noise_std=0.02)
    settings = CaptureSettings(
        center_frequency_hz=CENTER_HZ,
        sample_rate_hz=SAMPLE_RATE_HZ,
        duration_seconds=6.0,
        if_gain_reduction_db=30.0,
        agc=False,
    )
    result = run_capture_and_survey(
        tmp_path / "recording",
        tmp_path / "survey",
        capture=settings,
        band=_band_profile(),
        site=SiteProfile(site_id="fake_site", label="Fake site"),
        device=device,
        run_id="capture_test",
        database_path=tmp_path / "db.sqlite3",
        spectrum_fft_size=4096,
    )
    assert result["capture"]["frame_count"] == round(6.0 * SAMPLE_RATE_HZ)
    assert result["survey"]["run_id"] == "capture_test"
    assert result["survey"]["observation_count"] >= 1

    output_dir = Path(result["survey"]["output_dir"])
    assert (output_dir / "reports" / "report.md").is_file()
    assert (output_dir / "run.json").is_file()
    assert result["gps"]["source"] == "not_configured"


class StalledIqDevice:
    """Never delivers samples and never raises -- the field failure where a
    capture would otherwise hang forever with no output."""

    def open(self, settings: DeviceSettings) -> None:
        settings.validate()

    def read_stream_chunk(self, max_frames: int) -> np.ndarray:
        return np.empty(0, dtype=np.complex64)

    def close(self) -> None:
        pass


def test_stalled_device_hits_the_deadline_instead_of_hanging(tmp_path: Path) -> None:
    settings = CaptureSettings(
        center_frequency_hz=CENTER_HZ,
        sample_rate_hz=SAMPLE_RATE_HZ,
        duration_seconds=0.5,
        if_gain_reduction_db=20.0,
    )
    started = time.monotonic()
    manifest = run_capture(
        tmp_path,
        settings=settings,
        device=StalledIqDevice(),
        # Deadline = 0.5 * 0.2 + grace. Kept short so the test is fast; the
        # point under test is that the loop terminates at all.
        timeout_factor=0.2,
    )
    elapsed = time.monotonic() - started
    assert elapsed < 60, "the capture loop did not respect its deadline"
    assert manifest["timed_out"] is True
    assert manifest["complete"] is False
    assert manifest["frame_count"] == 0
    # Even a zero-length capture must leave a valid, parseable WAV behind.
    info = inspect_wave_iq(manifest["wav_path"])
    assert info.frame_count == 0


def test_on_progress_fires_even_when_every_read_comes_back_empty(tmp_path: Path) -> None:
    """The field app checks for job cancellation from inside `on_progress`.
    A device that is overflowing continuously returns an empty chunk on
    every read; that check must still run on every loop iteration, or the
    operator's Cancel button silently stops working for as long as the
    overflow storm lasts."""
    calls: list[int] = []
    settings = CaptureSettings(
        center_frequency_hz=CENTER_HZ,
        sample_rate_hz=SAMPLE_RATE_HZ,
        duration_seconds=0.5,
        if_gain_reduction_db=20.0,
    )
    run_capture(
        tmp_path,
        settings=settings,
        device=StalledIqDevice(),
        on_progress=lambda frames, target, _elapsed: calls.append(frames),
        timeout_factor=0.2,
    )
    assert len(calls) > 5, "on_progress must keep firing during a sustained empty-read streak"
    assert all(frames == 0 for frames in calls), "no frames were ever actually written"


def test_cancellation_from_on_progress_stops_an_overflow_storm_promptly(tmp_path: Path) -> None:
    """Reproduces the field failure directly: a capture overflowing
    continuously must still respond to cancellation quickly, not only once
    the (much longer, minutes-scale) wall-clock deadline elapses. Before the
    fix, a cancelling exception raised from `on_progress` could never fire
    here because the empty-chunk fast path skipped `on_progress` entirely."""

    class _Cancelled(Exception):
        pass

    calls: list[int] = []

    def on_progress(frames: int, target: int, elapsed: float) -> None:
        calls.append(frames)
        if len(calls) >= 5:
            raise _Cancelled("operator cancelled")

    settings = CaptureSettings(
        center_frequency_hz=CENTER_HZ,
        sample_rate_hz=SAMPLE_RATE_HZ,
        duration_seconds=1.0,
        if_gain_reduction_db=20.0,
    )
    started = time.monotonic()
    with pytest.raises(_Cancelled):
        run_capture(
            tmp_path,
            settings=settings,
            device=StalledIqDevice(),
            on_progress=on_progress,
            timeout_factor=1.0,
        )
    elapsed = time.monotonic() - started
    assert elapsed < 5.0, "cancellation must not wait for the overflow-storm deadline"
    assert len(calls) == 5


def test_progress_callback_reports_monotonic_advance(tmp_path: Path) -> None:
    """The field operator watches this for 90 seconds; it must actually
    move, and never go backwards."""
    seen: list[tuple[int, int]] = []
    settings = CaptureSettings(
        center_frequency_hz=CENTER_HZ,
        sample_rate_hz=SAMPLE_RATE_HZ,
        duration_seconds=0.5,
        if_gain_reduction_db=20.0,
        chunk_frames=8192,
    )
    run_capture(
        tmp_path,
        settings=settings,
        device=FakeIqDevice(),
        on_progress=lambda frames, target, _elapsed: seen.append((frames, target)),
    )
    assert len(seen) > 1
    assert [frames for frames, _ in seen] == sorted(frames for frames, _ in seen)
    assert seen[-1][0] == seen[-1][1] == round(0.5 * SAMPLE_RATE_HZ)


class GappyIqDevice:
    """Delivers data, but drops a buffer now and then like a real driver.

    `empty_every` reads come back empty, which is what SoapySDR returns on an
    overflow, and each costs `gap_seconds` of wall clock that no sample
    accounts for.
    """

    def __init__(self, *, empty_every: int = 4, gap_seconds: float = 0.02) -> None:
        self.reads = 0
        self.overflow_count = 0
        self._empty_every = empty_every
        self._gap_seconds = gap_seconds

    def open(self, settings: DeviceSettings) -> None:
        settings.validate()

    def read_stream_chunk(self, max_frames: int) -> np.ndarray:
        self.reads += 1
        if self.reads % self._empty_every == 0:
            self.overflow_count += 1
            time.sleep(self._gap_seconds)
            return np.empty(0, dtype=np.complex64)
        return np.zeros(max_frames, dtype=np.complex64)

    def close(self) -> None:
        pass


def test_the_manifest_reports_time_lost_not_only_overflow_count(tmp_path: Path) -> None:
    """How much time went missing is the number that matters downstream.

    A count says how often the driver dropped its FIFO, not how much was lost
    with it, and those differ by orders of magnitude: one overflow in a 30 s
    capture discards a buffer measured in milliseconds. Deciding whether a
    non-detection can be trusted needs the duration.
    """
    settings = CaptureSettings(
        center_frequency_hz=CENTER_HZ,
        sample_rate_hz=SAMPLE_RATE_HZ,
        duration_seconds=0.2,
        if_gain_reduction_db=20.0,
        chunk_frames=4096,
    )
    device = GappyIqDevice(empty_every=3, gap_seconds=0.05)
    manifest = run_capture(tmp_path, settings=settings, device=device)

    assert manifest["overflow_count"] > 0
    assert manifest["gap_seconds"] > 0.0, "sleeping through reads must register as lost time"
    assert 0.0 < manifest["time_coverage"] < 1.0
    # The span covers the samples plus the gaps, so the two must agree.
    assert manifest["stream_span_seconds"] >= manifest["actual_duration_seconds"]
    assert manifest["gap_seconds"] == pytest.approx(
        manifest["stream_span_seconds"] - manifest["actual_duration_seconds"], abs=1e-6
    )


def test_a_clean_capture_reports_full_time_coverage(tmp_path: Path) -> None:
    """The case the field stop was wrongly failed on.

    A capture with no gaps must not read as though it had any, or every
    non-detection it made is set aside for nothing.
    """
    settings = CaptureSettings(
        center_frequency_hz=CENTER_HZ,
        sample_rate_hz=SAMPLE_RATE_HZ,
        duration_seconds=0.2,
        if_gain_reduction_db=20.0,
    )
    manifest = run_capture(tmp_path, settings=settings, device=FakeIqDevice())
    assert manifest["overflow_count"] == 0
    assert manifest["time_coverage"] > 0.98, manifest["time_coverage"]
    assert manifest["gap_seconds"] < 0.05


def test_completed_capture_is_marked_complete_and_not_timed_out(tmp_path: Path) -> None:
    settings = CaptureSettings(
        center_frequency_hz=CENTER_HZ,
        sample_rate_hz=SAMPLE_RATE_HZ,
        duration_seconds=0.2,
        if_gain_reduction_db=20.0,
    )
    manifest = run_capture(tmp_path, settings=settings, device=FakeIqDevice())
    assert manifest["complete"] is True
    assert manifest["timed_out"] is False


class _FixedGpsResponseHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"latitude": 32.0853, "longitude": 34.7818}).encode("utf-8"))

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        pass


@pytest.fixture
def gps_server() -> Iterator[str]:
    server = HTTPServer(("127.0.0.1", 0), _FixedGpsResponseHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/location"
    finally:
        server.shutdown()
        thread.join()


def test_run_capture_and_survey_fetches_gps_from_url(tmp_path: Path, gps_server: str) -> None:
    device = FakeIqDevice()
    settings = CaptureSettings(
        center_frequency_hz=CENTER_HZ, sample_rate_hz=SAMPLE_RATE_HZ, duration_seconds=0.2, if_gain_reduction_db=20.0
    )
    result = run_capture_and_survey(
        tmp_path / "recording",
        tmp_path / "survey",
        capture=settings,
        band=_band_profile(),
        site=SiteProfile(site_id="fake_site", label="Fake site"),
        device=device,
        run_id="gps_test",
        database_path=tmp_path / "db.sqlite3",
        spectrum_fft_size=4096,
        gps_url=gps_server,
    )
    assert result["gps"]["source"] == "phone_gps"
    assert result["gps"]["latitude"] == 32.0853
    assert result["gps"]["longitude"] == 34.7818

    connection = connect_survey_database(tmp_path / "db.sqlite3")
    row = get_run(connection, "gps_test")
    connection.close()
    assert row is not None
    assert row["gps_source"] == "phone_gps"
    assert row["gps_latitude"] == 32.0853


def test_run_capture_and_survey_manual_gps_override_skips_fetch(tmp_path: Path, gps_server: str) -> None:
    device = FakeIqDevice()
    settings = CaptureSettings(
        center_frequency_hz=CENTER_HZ, sample_rate_hz=SAMPLE_RATE_HZ, duration_seconds=0.2, if_gain_reduction_db=20.0
    )
    result = run_capture_and_survey(
        tmp_path / "recording",
        tmp_path / "survey",
        capture=settings,
        band=_band_profile(),
        site=SiteProfile(site_id="fake_site", label="Fake site"),
        device=device,
        run_id="gps_override_test",
        database_path=tmp_path / "db.sqlite3",
        spectrum_fft_size=4096,
        gps_url=gps_server,
        gps_latitude=1.0,
        gps_longitude=2.0,
    )
    assert result["gps"]["source"] == "user"
    assert result["gps"]["latitude"] == 1.0
    assert result["gps"]["longitude"] == 2.0


def test_run_capture_and_survey_gps_fetch_failure_does_not_block_capture(tmp_path: Path) -> None:
    """GPS is supplementary: an unreachable phone server must never prevent
    the RF capture and survey from completing."""
    device = FakeIqDevice()
    settings = CaptureSettings(
        center_frequency_hz=CENTER_HZ, sample_rate_hz=SAMPLE_RATE_HZ, duration_seconds=0.2, if_gain_reduction_db=20.0
    )
    result = run_capture_and_survey(
        tmp_path / "recording",
        tmp_path / "survey",
        capture=settings,
        band=_band_profile(),
        site=SiteProfile(site_id="fake_site", label="Fake site"),
        device=device,
        run_id="gps_fail_test",
        database_path=tmp_path / "db.sqlite3",
        spectrum_fft_size=4096,
        gps_url="http://127.0.0.1:1/location",
        gps_timeout_seconds=1.0,
    )
    assert result["gps"]["source"] == "fetch_failed"
    assert result["gps"]["error"]
    assert result["survey"]["observation_count"] >= 0
    assert Path(result["capture"]["wav_path"]).is_file()


def test_capture_time_reaches_the_database_from_the_auxi_chunk(tmp_path: Path) -> None:
    """Capture time is how a run is later matched to where it was made, so
    it has to be the real wall-clock time of the recording and it has to be
    sourced from the auxi chunk this writer produces -- not from import
    time, and not from a filename guess."""
    from datetime import UTC, datetime, timedelta

    from dmr_iq_surveyor.survey.store import connect_survey_database, get_run

    before = datetime.now(UTC)
    settings = CaptureSettings(
        center_frequency_hz=CENTER_HZ,
        sample_rate_hz=SAMPLE_RATE_HZ,
        duration_seconds=0.2,
        if_gain_reduction_db=25.0,
        lna_state=2,
    )
    run_capture_and_survey(
        tmp_path / "recording",
        tmp_path / "survey",
        capture=settings,
        band=_band_profile(),
        site=SiteProfile(site_id="kit", label="Kit"),
        device=FakeIqDevice(),
        run_id="timed",
        database_path=tmp_path / "db.sqlite3",
        spectrum_fft_size=4096,
        site_id_override="park1",
    )
    after = datetime.now(UTC)

    connection = connect_survey_database(tmp_path / "db.sqlite3")
    row = get_run(connection, "timed")
    connection.close()
    assert row is not None
    assert row["capture_time_source"] == "auxi"
    stored = datetime.fromisoformat(row["capture_start_utc"])
    # auxi's SYSTEMTIME stores milliseconds only (floor, no microseconds),
    # so the stored value can read up to ~1ms earlier than a
    # microsecond-precision "before" taken just ahead of it -- not a defect
    # in what's being tested, just the auxi format's resolution.
    assert before - timedelta(milliseconds=1) <= stored <= after
    # The site id is the other half of the join key for a location table
    # assembled after the fact.
    assert row["site_id"] == "park1"
