"""Device state the field app can always answer with.

The failure these pin: `GET /api/state` used to call `probe_soapysdr()`
inline, so a wedged SDRplay API service made every state request hang --
with no timeout, no retry and, because BaseHTTPRequestHandler only logs
from send_response(), no log line either. The phone sat on "connecting…"
and "disk…" indefinitely and no recording was ever made.

Nothing here opens an SDR or starts a probe process.
"""

from __future__ import annotations

import inspect
import json
import subprocess
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import pytest
from fixtures.device_probe import StubProbeRunner, mocked_absent, present
from fixtures.geo_scenario import Transmitter, build_database, seed_run
from fixtures.live_profiles import write_profiles

from dmr_iq_surveyor.capture._soapy_probe import PROBE_DISCONNECTED, PROBE_NOT_SUPPORTED
from dmr_iq_surveyor.capture.probe import (
    REASON_RUNNER_RAISED,
    REASON_SPAWN_FAILED,
    STATE_AVAILABLE,
    STATE_DISCONNECTED,
    STATE_FAILED,
    STATE_NOT_SUPPORTED,
    STATE_TIMED_OUT,
    ProbeOutcome,
    SubprocessProbeRunner,
)
from dmr_iq_surveyor.web.devices import (
    AVAILABLE_TTL_SECONDS,
    DISCONNECTED_BACKOFF_SECONDS,
    STATE_BUSY,
    STATE_CHECKING,
    DeviceMonitor,
    DeviceSnapshot,
)
from dmr_iq_surveyor.web.jobs import STATUS_SUCCEEDED
from dmr_iq_surveyor.web.server import create_server
from dmr_iq_surveyor.web.service import FieldSettings

SITE = Transmitter(867_762_500.0, 32.050, 34.800, reference_level_db=25.0)
STOPS = [(32.045, 34.795), (32.056, 34.806), (32.041, 34.809)]

CONTRACT_KEYS = {
    "state",
    "available",
    "probe_error",
    "resolved_label",
    "checked_at",
    "age_seconds",
    "stale",
    "devices_found",
    "reason",
    "probe_seconds",
    "last_known_label",
    "refreshing",
}


class _Clock:
    """A clock the test moves by hand, so ageing is exact and instant."""

    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _settled(monitor: DeviceMonitor, timeout: float = 10.0) -> None:
    """Wait for the monitor's first probe to land."""
    deadline = time.monotonic() + timeout
    while monitor.probe_count == 0 and time.monotonic() < deadline:
        time.sleep(0.005)
    assert monitor.probe_count >= 1, "the initial probe never completed"


def _monitor(runner: StubProbeRunner, **kwargs: object) -> DeviceMonitor:
    return DeviceMonitor(runner, driver="sdrplay", **kwargs)  # type: ignore[arg-type]


def _wait_for_probes(monitor: DeviceMonitor, count: int, timeout: float = 10.0) -> None:
    """Wait until the monitor has *recorded* `count` probes.

    Recorded, not started: the probe thread timestamps its result when it
    stores it, so a test that advanced its clock the moment the runner was
    entered would be dating that result from the future.
    """
    deadline = time.monotonic() + timeout
    while monitor.probe_count < count and time.monotonic() < deadline:
        time.sleep(0.005)
    assert monitor.probe_count >= count, f"expected {count} probes, saw {monitor.probe_count}"


# -- the API keeps answering -------------------------------------------------


def _server(tmp_path: Path, runner: StubProbeRunner):
    database = tmp_path / "db.sqlite3"
    connection = build_database(database)
    for index, (latitude, longitude) in enumerate(STOPS):
        seed_run(
            connection,
            run_id=f"run_{index}",
            latitude=latitude,
            longitude=longitude,
            transmitters=[SITE],
            capture_start_utc=f"2026-08-01T{8 + index:02d}:00:00+00:00",
        )
    connection.close()
    settings = FieldSettings(
        database_path=database,
        output_root=tmp_path / "out",
        recordings_dir=tmp_path / "rec",
        token="s3cret",
    )
    return create_server(settings, host="127.0.0.1", port=0, probe_runner=runner)


@pytest.fixture()
def hung_server(tmp_path: Path) -> Iterator[tuple[str, StubProbeRunner]]:
    """A server whose SDR probe never answers -- the field failure, exactly."""
    runner = StubProbeRunner(gate=threading.Event())
    server = _server(tmp_path, runner)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", runner
    finally:
        server.shutdown()
        server.server_close()


def _get(base: str, path: str, timeout: float = 5.0) -> tuple[int, dict]:
    request = urllib.request.Request(base + path)
    request.add_header("X-Auth-Token", "s3cret")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read().decode()
        return response.status, json.loads(body) if body else {}


def test_state_answers_while_the_sdr_probe_is_stuck(
    hung_server: tuple[str, StubProbeRunner],
) -> None:
    base, runner = hung_server
    assert runner.entered.wait(10.0), "the probe never started"

    started = time.monotonic()
    status, payload = _get(base, "/api/state")
    elapsed = time.monotonic() - started

    assert status == 200
    assert elapsed < 2.0, f"/api/state took {elapsed:.1f}s while the probe was stuck"
    assert payload["device"]["state"] == STATE_CHECKING
    assert payload["device"]["available"] is False


def test_the_rest_of_the_state_is_complete_while_the_probe_is_stuck(
    hung_server: tuple[str, StubProbeRunner],
) -> None:
    """The phone showed "connecting…" and "disk…" because one blocked call
    took the whole payload down with it. Everything that does not depend on
    the SDR must arrive regardless."""
    base, runner = hung_server
    assert runner.entered.wait(10.0)
    _status, payload = _get(base, "/api/state")

    assert payload["disk"]["free_bytes"] > 0
    assert payload["settings"]["sample_rate_hz"] > 0
    assert isinstance(payload["sites"], list) and payload["sites"]
    assert isinstance(payload["stops"], list)
    assert "plan" in payload and "jobs" in payload and "live" in payload
    assert set(payload["device"]) == CONTRACT_KEYS


def test_repeated_state_requests_do_not_stack_up_probes(
    hung_server: tuple[str, StubProbeRunner],
) -> None:
    """One stuck probe, however many people are looking. A thread per poll
    is how a phone that reloads a few times exhausts a Raspberry Pi."""
    base, runner = hung_server
    assert runner.entered.wait(10.0)
    before = threading.active_count()

    errors: list[BaseException] = []

    def _hit() -> None:
        try:
            _get(base, "/api/state")
        except BaseException as exc:  # noqa: BLE001 - the test reports, not handles
            errors.append(exc)

    workers = [threading.Thread(target=_hit) for _ in range(20)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=15.0)

    assert not errors, f"state requests failed: {errors[:3]}"
    assert runner.calls == 1, f"{runner.calls} probes were started, not 1"
    assert threading.active_count() <= before + 2


def test_the_sites_endpoint_does_not_probe(
    hung_server: tuple[str, StubProbeRunner],
) -> None:
    """/api/sites used to build the entire state payload -- SDR probe
    included -- and throw all but the sites away."""
    base, runner = hung_server
    assert runner.entered.wait(10.0)
    status, payload = _get(base, "/api/sites")
    assert status == 200
    assert isinstance(payload["sites"], list)
    assert runner.calls == 1


# -- the contract, state by state -------------------------------------------


def test_a_snapshot_is_a_memory_read_not_a_probe() -> None:
    """No grace period, no waiting for hardware: the answer is whatever is
    in memory right now, returned immediately."""
    runner = StubProbeRunner(gate=threading.Event())
    monitor = _monitor(runner)
    try:
        assert runner.entered.wait(10.0)
        for _ in range(200):
            started = time.monotonic()
            snapshot = monitor.snapshot()
            assert time.monotonic() - started < 0.05
        assert snapshot.state == STATE_CHECKING
        assert runner.calls == 1, "snapshot() started a probe"
    finally:
        monitor.close()


def test_checking_says_so_rather_than_claiming_a_device() -> None:
    runner = StubProbeRunner(gate=threading.Event())
    monitor = _monitor(runner)
    try:
        snapshot = monitor.snapshot()
        assert snapshot.state == STATE_CHECKING
        assert snapshot.available is False
        assert snapshot.probe_error
        assert snapshot.resolved_label is None
        assert snapshot.checked_at is None
        assert snapshot.age_seconds is None
        assert snapshot.stale is False
        assert snapshot.devices_found == []
    finally:
        monitor.close()


def test_available_never_travels_without_its_age() -> None:
    """A cached "available" that does not say when it was measured is how a
    stale reading gets mistaken for a fresh one."""
    clock = _Clock()
    monitor = _monitor(StubProbeRunner(present("SDRplay RSP1A")), clock=clock)
    try:
        _settled(monitor)
        snapshot = monitor.snapshot()
        assert snapshot.state == STATE_AVAILABLE
        assert snapshot.available is True
        assert snapshot.probe_error is None
        assert snapshot.resolved_label == "SDRplay RSP1A"
        assert snapshot.checked_at is not None
        assert snapshot.age_seconds == 0.0
        assert snapshot.stale is False
        assert snapshot.devices_found == [{"driver": "sdrplay", "label": "SDRplay RSP1A"}]

        clock.advance(AVAILABLE_TTL_SECONDS + 1.0)
        aged = monitor.snapshot()
        assert aged.available is True
        assert aged.stale is True
        assert aged.age_seconds == pytest.approx(AVAILABLE_TTL_SECONDS + 1.0)
    finally:
        monitor.close()


def test_disconnected_carries_the_probes_own_explanation() -> None:
    monitor = _monitor(StubProbeRunner())
    try:
        _settled(monitor)
        snapshot = monitor.snapshot()
        assert snapshot.state == STATE_DISCONNECTED
        assert snapshot.available is False
        assert "mocked out" in snapshot.probe_error
        assert snapshot.reason == PROBE_DISCONNECTED
        assert snapshot.resolved_label is None
        assert snapshot.checked_at is not None
    finally:
        monitor.close()


def test_a_spawn_failure_is_reported_as_a_failure_not_an_absence() -> None:
    """"The probe could not be started" is not "there is no SDR"."""
    outcome = ProbeOutcome(
        state=STATE_FAILED,
        reason=REASON_SPAWN_FAILED,
        detail="could not start the SDR probe process (OSError: nope).",
    )
    monitor = _monitor(StubProbeRunner(outcome))
    try:
        _settled(monitor)
        snapshot = monitor.snapshot()
        assert snapshot.state == STATE_FAILED
        assert snapshot.available is False
        assert snapshot.reason == REASON_SPAWN_FAILED
        assert "could not start" in snapshot.probe_error
    finally:
        monitor.close()


def test_missing_bindings_are_reported_as_not_supported() -> None:
    outcome = ProbeOutcome(
        state=STATE_NOT_SUPPORTED,
        reason=PROBE_NOT_SUPPORTED,
        detail="SoapySDR Python bindings are not importable. Run pi_soapysdr_setup.sh.",
    )
    monitor = _monitor(StubProbeRunner(outcome))
    try:
        _settled(monitor)
        snapshot = monitor.snapshot()
        assert snapshot.state == STATE_NOT_SUPPORTED
        assert snapshot.available is False
        assert "pi_soapysdr_setup.sh" in snapshot.probe_error
    finally:
        monitor.close()


def test_a_timed_out_probe_is_always_stale_and_never_retried_on_its_own() -> None:
    """Another automatic attempt risks a second stuck child. The way back is
    a deliberate rescan, not a timer."""
    clock = _Clock()
    outcome = ProbeOutcome(state=STATE_TIMED_OUT, detail="the SDR check did not finish in 8 s.")
    runner = StubProbeRunner(outcome)
    monitor = _monitor(runner, clock=clock)
    try:
        _settled(monitor)
        snapshot = monitor.snapshot()
        assert snapshot.state == STATE_TIMED_OUT
        assert snapshot.available is False
        assert snapshot.stale is True

        clock.advance(86_400.0)
        assert monitor.refresh_if_due() is False
        assert runner.calls == 1
    finally:
        monitor.close()


def test_busy_does_not_pass_off_an_older_reading_as_a_current_one() -> None:
    """While a capture holds the SDR the app knows nothing current about it,
    and says exactly that -- no label, no device list, never "available"."""
    held = {"value": False}
    runner = StubProbeRunner(present("SDRplay RSP1A"))
    monitor = _monitor(runner, device_held=lambda: held["value"])
    try:
        _settled(monitor)
        assert monitor.snapshot().state == STATE_AVAILABLE

        held["value"] = True
        snapshot = monitor.snapshot()
        assert snapshot.state == STATE_BUSY
        assert snapshot.available is False
        assert snapshot.resolved_label is None
        assert snapshot.devices_found == []
        assert snapshot.stale is True
        assert snapshot.checked_at is not None, "the last real probe is still dated"
        assert snapshot.last_known_label == "SDRplay RSP1A"
        assert "holds the SDR" in snapshot.probe_error

        assert monitor.refresh_if_due() is False
        assert runner.calls == 1, "the device was probed while a capture held it"
    finally:
        monitor.close()


# -- when a new probe is, and is not, started -------------------------------


def test_an_available_answer_is_reused_until_its_window_passes() -> None:
    clock = _Clock()
    runner = StubProbeRunner(present())
    monitor = _monitor(runner, clock=clock)
    try:
        _settled(monitor)
        for _ in range(10):
            monitor.poll()
        assert runner.calls == 1, "a probe per poll"

        clock.advance(AVAILABLE_TTL_SECONDS - 1.0)
        monitor.poll()
        assert runner.calls == 1

        clock.advance(2.0)
        monitor.poll()
        _wait_for_probes(monitor, 2)
        assert runner.calls == 2
    finally:
        monitor.close()


def test_a_disconnected_sdr_is_retried_on_a_widening_backoff() -> None:
    """Someone is standing there with the cable, so the first retry is
    quick; a drive that lasts an hour must not pay for that forever."""
    clock = _Clock()
    runner = StubProbeRunner()
    monitor = _monitor(runner, clock=clock)
    try:
        _settled(monitor)
        for index, window in enumerate(DISCONNECTED_BACKOFF_SECONDS, start=1):
            clock.advance(window - 1.0)
            monitor.poll()
            assert runner.calls == index, f"probed early inside the {window:g}s window"

            clock.advance(2.0)
            monitor.poll()
            _wait_for_probes(monitor, index + 1)
            assert runner.calls == index + 1, f"no retry after the {window:g}s window"
    finally:
        monitor.close()


def test_the_backoff_resets_when_the_answer_changes() -> None:
    clock = _Clock()
    runner = StubProbeRunner()
    monitor = _monitor(runner, clock=clock)
    try:
        _settled(monitor)
        clock.advance(DISCONNECTED_BACKOFF_SECONDS[0] + 1.0)
        monitor.poll()
        _wait_for_probes(monitor, 2)

        # Now the cable goes back in, and then out again: the widened window
        # must not still apply to the fresh disconnection.
        runner.set_outcome(present())
        clock.advance(DISCONNECTED_BACKOFF_SECONDS[1] + 1.0)
        monitor.poll()
        _wait_for_probes(monitor, 3)
        assert monitor.snapshot().state == STATE_AVAILABLE

        runner.set_outcome(mocked_absent)
        clock.advance(AVAILABLE_TTL_SECONDS + 1.0)
        monitor.poll()
        _wait_for_probes(monitor, 4)
        assert monitor.snapshot().state == STATE_DISCONNECTED

        # One disconnection in a row, so the shortest window applies again.
        clock.advance(DISCONNECTED_BACKOFF_SECONDS[0] + 1.0)
        monitor.poll()
        _wait_for_probes(monitor, 5)
        assert runner.calls == 5, "the backoff kept widening across a state change"
    finally:
        monitor.close()


# -- lifecycle ---------------------------------------------------------------


def test_closing_the_monitor_ends_its_thread_and_releases_the_runner() -> None:
    runner = StubProbeRunner(gate=threading.Event())
    before = threading.active_count()
    monitor = _monitor(runner)
    assert runner.entered.wait(10.0)

    monitor.close()

    assert runner.closed is True
    deadline = time.monotonic() + 10.0
    while threading.active_count() > before and time.monotonic() < deadline:
        time.sleep(0.01)
    assert threading.active_count() <= before, "the probe thread outlived the monitor"
    assert monitor.request_refresh(force=True) is False, "a closed monitor started a probe"


def test_a_server_cycle_leaves_no_threads_behind(tmp_path: Path) -> None:
    """A server per test, fifty tests: a monitor thread that survives
    shutdown is a leak the suite would carry to the end."""
    before = threading.active_count()
    for index in range(3):
        runner = StubProbeRunner(gate=threading.Event())
        server = _server(tmp_path / f"cycle{index}", runner)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        assert runner.entered.wait(10.0)
        _get(f"http://127.0.0.1:{server.server_address[1]}", "/api/state")
        server.shutdown()
        server.server_close()
        assert runner.closed is True, "server_close() did not close the probe runner"

    deadline = time.monotonic() + 10.0
    while threading.active_count() > before and time.monotonic() < deadline:
        time.sleep(0.02)
    assert threading.active_count() <= before, "threads survived the server"


# -- Rescan SDR --------------------------------------------------------------


def _post(base: str, path: str, body: dict | None = None, timeout: float = 5.0) -> tuple[int, dict]:
    request = urllib.request.Request(
        base + path,
        method="POST",
        data=json.dumps(body or {}).encode(),
    )
    request.add_header("X-Auth-Token", "s3cret")
    request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read().decode()
            return response.status, json.loads(payload) if payload else {}
    except urllib.error.HTTPError as error:
        payload = error.read().decode()
        return error.code, json.loads(payload) if payload else {}


def test_rescan_asks_for_a_probe_and_answers_without_waiting_for_it(tmp_path: Path) -> None:
    """Plugging the RSP1A back in must not mean restarting the whole app."""
    runner = StubProbeRunner()
    server = _server(tmp_path, runner)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        _settled(server.service.devices)
        assert runner.calls == 1

        runner.set_outcome(present("SDRplay RSP1A"))
        started = time.monotonic()
        status, payload = _post(base, "/api/device/rescan")
        assert time.monotonic() - started < 2.0
        assert status == 200
        assert payload["rescan_started"] is True
        assert payload["rescan_declined_reason"] is None
        assert set(payload["device"]) == CONTRACT_KEYS

        _wait_for_probes(server.service.devices, 2)
        assert _get(base, "/api/state")[1]["device"]["state"] == STATE_AVAILABLE
    finally:
        server.shutdown()
        server.server_close()


def test_rescan_will_not_stack_a_second_probe_on_a_stuck_one(
    hung_server: tuple[str, StubProbeRunner],
) -> None:
    """The button must not become a way to make more stuck probes."""
    base, runner = hung_server
    assert runner.entered.wait(10.0)

    status, payload = _post(base, "/api/device/rescan")
    assert status == 200
    assert payload["rescan_started"] is False
    assert "already running" in payload["rescan_declined_reason"]
    assert runner.calls == 1


# -- readiness before the device is opened -----------------------------------


def _capture_service(tmp_path: Path, runner: StubProbeRunner):
    from dmr_iq_surveyor.web.service import FieldService

    database = tmp_path / "db.sqlite3"
    build_database(database).close()
    service = FieldService(
        FieldSettings(
            database_path=database,
            output_root=tmp_path / "out",
            recordings_dir=tmp_path / "rec",
        ),
        probe_runner=runner,
    )
    service.set_position({"latitude": 32.05, "longitude": 34.8})
    return service


def test_a_capture_rechecks_the_device_rather_than_trusting_the_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The state page may be showing a reading minutes old. That is fine for
    a status pill and not fine for the moment before the SDR is opened."""
    import dmr_iq_surveyor.web.service as web_service

    monkeypatch.setattr(web_service, "READINESS_MAX_AGE_SECONDS", 0.0)
    runner = StubProbeRunner(present("SDRplay RSP1A"))
    service = _capture_service(tmp_path, runner)
    try:
        _settled(service.devices)
        assert runner.calls == 1

        with pytest.raises((RuntimeError, ValueError)):
            # Fails later, on the site profile a clean checkout does not
            # carry -- but only after the device has been re-checked.
            service.start_capture({"duration_seconds": 5})
        assert runner.calls == 2, "the capture path reused a cached reading"
    finally:
        service.close()


def test_a_readiness_check_that_cannot_finish_refuses_without_starting_a_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A wedged SDR must produce a sentence the operator can act on, not a
    job that starts and then hangs."""
    import dmr_iq_surveyor.web.service as web_service

    monkeypatch.setattr(web_service, "READINESS_WAIT_SECONDS", 0.3)
    runner = StubProbeRunner(gate=threading.Event())
    service = _capture_service(tmp_path, runner)
    try:
        assert runner.entered.wait(10.0)
        started = time.monotonic()
        with pytest.raises(RuntimeError) as raised:
            service.start_capture({"duration_seconds": 5})
        elapsed = time.monotonic() - started

        assert elapsed < 5.0
        assert "readiness check did not finish" in str(raised.value)
        assert service.jobs.list() == [], "a job was created despite the refusal"
        assert runner.calls == 1, "a second probe was stacked on the stuck one"
    finally:
        service.close()


def test_a_drive_makes_the_same_fresh_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import dmr_iq_surveyor.web.service as web_service

    monkeypatch.setattr(web_service, "READINESS_WAIT_SECONDS", 0.3)
    runner = StubProbeRunner(gate=threading.Event())
    service = _capture_service(tmp_path, runner)
    try:
        assert runner.entered.wait(10.0)
        service.push_live_position({"latitude": 32.05, "longitude": 34.8, "accuracy_m": 6.0})
        with pytest.raises(RuntimeError) as raised:
            service.start_live({"max_seconds": 30.0})
        assert "readiness check did not finish" in str(raised.value)
        assert service.jobs.list() == []
    finally:
        service.close()


def test_a_full_card_is_still_reported_before_the_device_is_checked(
    tmp_path: Path,
) -> None:
    """Order matters: a full card reported as an SDR fault sends the
    operator off to diagnose the wrong thing."""
    runner = StubProbeRunner(present("SDRplay RSP1A"))
    service = _capture_service(tmp_path, runner)
    try:
        _settled(service.devices)
        before = runner.calls
        with pytest.raises(RuntimeError) as raised:
            # More than any test machine has free, so disk_status refuses.
            service.start_capture({"duration_seconds": 5, "sample_rate_hz": 10_000_000_000.0})
        message = str(raised.value)
        assert "SDR" not in message, message
        assert runner.calls == before, "the device was probed before the card was checked"
    finally:
        service.close()



# -- the redundant in-job probe is gone (fix(web): remove the redundant
#    in-job device probe) -------------------------------------------------


def _working_capture_service(tmp_path: Path, runner: StubProbeRunner):
    """A FieldService whose profiles actually resolve, so start_capture()
    can get all the way to submitting a job -- unlike `_capture_service`
    above, which deliberately fails at profile resolution."""
    from dmr_iq_surveyor.web.service import FieldService

    database = tmp_path / "db.sqlite3"
    build_database(database).close()
    band, site = write_profiles(tmp_path / "profiles", center_hz=868_000_000.0)
    service = FieldService(
        FieldSettings(
            database_path=database,
            output_root=tmp_path / "out",
            recordings_dir=tmp_path / "rec",
            band=str(band),
            site_profile=str(site),
        ),
        probe_runner=runner,
    )
    service.set_position({"latitude": 32.05, "longitude": 34.8})
    return service


def _fake_run_capture(output_dir, *, settings, filename=None, **_kwargs):
    """Stands in for capture.core.run_capture: no device, no real file, an
    instantly "complete" manifest with the shape _capture_and_analyse reads."""
    return {
        "wav_path": Path(output_dir) / (filename or "fake.wav"),
        "timed_out": False,
        "overflow_count": 0,
        "actual_duration_seconds": settings.duration_seconds,
        "time_coverage": 1.0,
        "gap_seconds": 0.0,
        "complete": True,
    }


def _fake_run_survey(*_args, **_kwargs):
    return {"observation_count": 0, "coverage_status": "not_covered", "drive_view": None}


def _fake_materialise_measurements(*_args, **_kwargs):
    return {"summary": {"detections": 0, "non_detections": 0, "not_covered": 0}}


def _wait_for_terminal(job, timeout: float = 10.0):
    deadline = time.monotonic() + timeout
    while not job.is_terminal() and time.monotonic() < deadline:
        time.sleep(0.005)
    assert job.is_terminal(), f"the job never reached a terminal state (stuck at {job.stage!r})"
    return job


def test_a_successful_capture_probes_the_device_once_and_the_job_completes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The behavioural proof that the in-job probe is gone.

    Counts calls to require_device_ready() itself, not just probe
    subprocesses: under the default (non-zero) freshness window, a
    reintroduced in-job check would almost always reuse the same cached
    reading as the pre-submit one and never show up as a second probe --
    which is exactly why the original ignore_held=True call went untested
    for as long as it did (see the commit message). Counting the method
    call directly is what actually catches it coming back.

    The capture/survey/measurement pipeline is faked out (run_capture,
    run_survey, materialise_measurements) so the job can reach a real
    terminal state without any SDR or disk I/O.
    """
    import dmr_iq_surveyor.web.service as web_service
    from dmr_iq_surveyor.web.service import FieldService

    monkeypatch.setattr(web_service, "run_capture", _fake_run_capture)
    monkeypatch.setattr(web_service, "run_survey", _fake_run_survey)
    monkeypatch.setattr(web_service, "materialise_measurements", _fake_materialise_measurements)

    readiness_calls: list[None] = []
    original_require_ready = FieldService.require_device_ready

    def _counting_require_ready(self: FieldService) -> None:
        readiness_calls.append(None)
        original_require_ready(self)

    monkeypatch.setattr(FieldService, "require_device_ready", _counting_require_ready)

    runner = StubProbeRunner(present("SDRplay RSP1A 230405A498"))
    service = _working_capture_service(tmp_path, runner)
    try:
        _settled(service.devices)
        assert runner.calls == 1, "the initial background probe did not land"

        job = service.start_capture({"duration_seconds": 1.0, "solve": False})
        assert len(readiness_calls) == 1, (
            "start_capture() must call require_device_ready() exactly once"
        )
        assert runner.calls == 1, "a fresh reading was available; this should not have re-probed"

        _wait_for_terminal(job)
        assert job.status == STATUS_SUCCEEDED, job.error

        assert len(readiness_calls) == 1, (
            "require_device_ready() was called again while the job was executing -- "
            "the in-job check is back"
        )
        assert runner.calls == 1, "a probe ran while the job was executing"
    finally:
        service.close()


def test_the_ignore_held_parameter_no_longer_exists() -> None:
    """Supplementary only: the behavioural test above is what actually
    proves the second probe is gone. This just pins that the bypass it used
    -- a boolean unrelated to which job was asking -- has no seam left to
    reintroduce it through."""
    from dmr_iq_surveyor.web.service import FieldService

    assert "ignore_held" not in inspect.signature(DeviceMonitor.ensure_fresh).parameters
    assert "ignore_held" not in inspect.signature(FieldService.require_device_ready).parameters



# -- requirement 1: force=True must not probe a held device -----------------


def test_rescan_during_an_active_capture_is_refused_and_never_touches_the_runner(
    tmp_path: Path,
) -> None:
    """A capture in progress holds the SDR; the button that is supposed to
    help with a *disconnected* device must not itself go poking at one that
    is in use."""
    runner = StubProbeRunner(present("SDRplay RSP1A"))
    service = _capture_service(tmp_path, runner)
    try:
        _settled(service.devices)
        before = runner.calls

        release = threading.Event()
        job = service.jobs.submit(
            kind="capture", label="fake capture", work=lambda job: (release.wait(10.0), {})[1]
        )
        try:
            result = service.rescan_device()
            assert result["rescan_started"] is False
            assert result["rescan_declined_reason"]
            assert result["device"]["state"] == STATE_BUSY
            assert runner.calls == before, "a probe ran while a job held the device"
        finally:
            release.set()
            _wait_for_terminal(job)
    finally:
        service.close()


def test_request_refresh_force_true_itself_refuses_a_held_device() -> None:
    """FieldService.rescan_device() already declines before calling
    request_refresh() at all (via refresh_declined_reason()) -- this pins
    that request_refresh(force=True) refuses on its own too, for any other
    caller that reaches it directly, rather than depending on every caller
    to check device_held() first."""
    held = {"value": True}
    runner = StubProbeRunner(present("SDRplay RSP1A"))
    monitor = _monitor(runner, device_held=lambda: held["value"])
    try:
        assert runner.calls == 0, "the constructor probed a device held from the start"
        assert monitor.request_refresh(force=True) is False
        assert runner.calls == 0, "request_refresh(force=True) probed a held device directly"

        held["value"] = False
        assert monitor.request_refresh(force=True) is True
        _wait_for_probes(monitor, 1)
        assert monitor.snapshot().state == STATE_AVAILABLE
    finally:
        monitor.close()


# -- requirement 2: ensure_fresh() must not probe a held device -------------


def test_ensure_fresh_returns_busy_without_probing_when_the_device_is_held() -> None:
    """Even the monitor's OWN construction-time refresh must respect a
    device that is held from the very start -- not just a later Rescan."""
    held = {"value": True}
    runner = StubProbeRunner(present("SDRplay RSP1A"))
    monitor = _monitor(runner, device_held=lambda: held["value"])
    try:
        assert runner.calls == 0, "the constructor probed a device held from the start"

        snapshot = monitor.ensure_fresh(max_age=5.0, wait=0.2)
        assert snapshot.state == STATE_BUSY
        assert snapshot.available is False
        assert runner.calls == 0, "ensure_fresh probed a held device"

        # The device is freed; a later, ordinary call still works normally,
        # proving the monitor was never wedged by having been held.
        held["value"] = False
        assert monitor.request_refresh(force=True) is True
        _wait_for_probes(monitor, 1)
        assert monitor.snapshot().state == STATE_AVAILABLE
    finally:
        monitor.close()


# -- requirement 3: DeviceMonitor defers entirely to the runner's own -------
# -- orphan handling; recovery needs no restart -----------------------------


class _RevivableStuckPopen:
    """A child that ignores kill() until revive() is called -- the kernel
    finally releasing a process stuck in an uninterruptible wait. Local to
    this file on purpose: it exercises DeviceMonitor's integration with the
    REAL SubprocessProbeRunner, not a stand-in for either of them."""

    def __init__(self) -> None:
        self.stdout = self.stderr = self.stdin = None
        self.returncode: int | None = None
        self.kills = 0

    def kill(self) -> None:
        self.kills += 1

    def communicate(self, timeout: float | None = None) -> tuple[str, str]:
        if self.returncode is None:
            raise subprocess.TimeoutExpired(cmd="fake", timeout=timeout or 0.0)
        return "", ""

    def poll(self) -> int | None:
        return self.returncode

    def revive(self) -> None:
        self.returncode = -9


class _GoodPopen:
    """A normal, fast, successful child -- the replacement spawned once the
    earlier stuck one is finally reaped. A distinct class from
    `_RevivableStuckPopen` on purpose: reusing the same object across both
    spawns would leave the "second child returns a real device" half of
    this test unproven."""

    def __init__(self, payload: dict) -> None:
        self.stdout = self.stderr = self.stdin = None
        self.returncode: int | None = 0
        self._payload = payload

    def kill(self) -> None:
        pass

    def communicate(self, timeout: float | None = None) -> tuple[str, str]:
        return json.dumps(self._payload), ""

    def poll(self) -> int | None:
        return self.returncode


_GOOD_PAYLOAD = {
    "available": True,
    "requested_driver": "sdrplay",
    "resolved_label": "SDRplay RSP1A 230405A498",
    "probe_error": None,
    "devices_found": [{"driver": "sdrplay", "label": "SDRplay RSP1A 230405A498"}],
    "reason": None,
}


def test_an_orphan_that_finally_exits_recovers_on_rescan_without_a_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DeviceMonitor never asks the runner "are you orphaned" -- it just
    starts a probe via request_refresh(force=True) and trusts whatever the
    runner's own run() reports. The real SubprocessProbeRunner is used here
    specifically to prove that integration holds with the getattr("orphaned")
    check gone from DeviceMonitor: a first child gets stuck then finally
    exits, a second, separate child is spawned and reports a real device,
    and at no point does a second child exist while the first is still
    unaccounted for."""
    import dmr_iq_surveyor.capture.probe as probe_module

    stuck = _RevivableStuckPopen()
    spawned: list[object] = []

    def _popen(*_args: object, **_kwargs: object) -> object:
        if not spawned:
            spawned.append(stuck)
            return stuck
        # A second spawn must only ever happen once the first child is
        # confirmed dead -- _orphan_verdict() clears self._orphan only
        # after poll() stops returning None. This is the "never more than
        # one active/stuck child" guarantee, checked at the moment it would
        # actually be violated, not just at the end.
        assert stuck.poll() is not None, "a second child was spawned while the first was still live"
        good = _GoodPopen(_GOOD_PAYLOAD)
        spawned.append(good)
        return good

    monkeypatch.setattr(probe_module.subprocess, "Popen", _popen)
    runner = SubprocessProbeRunner()
    monitor = _monitor(runner, timeout_seconds=0.05)
    try:
        _wait_for_probes(monitor, 1)
        assert monitor.snapshot().state == STATE_TIMED_OUT
        assert len(spawned) == 1
        assert runner.orphaned is True

        # DeviceMonitor does not pre-emptively decline here: no thread is
        # currently alive, so it has no reason of its own to refuse. It is
        # the runner's own _orphan_verdict, invoked a moment later inside
        # request_refresh(), that safely says no to a second child.
        assert monitor.refresh_declined_reason() is None, (
            "the monitor declined based on runner-internal orphan state it has no business knowing"
        )

        # Still stuck: Rescan must not spawn a second child while this one
        # is unaccounted for.
        assert monitor.request_refresh(force=True) is True
        _wait_for_probes(monitor, 2)
        assert monitor.snapshot().state == STATE_TIMED_OUT
        assert len(spawned) == 1, "a second child was spawned while the first was still stuck"
        assert runner.orphaned is True

        # The kernel finally lets it go -- no restart of anything involved.
        stuck.revive()
        assert monitor.request_refresh(force=True) is True
        _wait_for_probes(monitor, 3)

        assert len(spawned) == 2, "no fresh child was spawned once the orphan was reaped"
        final = monitor.snapshot()
        assert final.state == STATE_AVAILABLE
        assert final.resolved_label == "SDRplay RSP1A 230405A498"
        assert final.available is True
        assert runner.orphaned is False
    finally:
        monitor.close()


# -- requirement 4: "refreshing", and waiting for an in-flight probe --------


def _settled_and_not_refreshing(
    monitor: DeviceMonitor, *, not_checked_at: str | None = None, timeout: float = 10.0
) -> DeviceSnapshot:
    """A snapshot that has BOTH landed (a fresh `checked_at`, when
    `not_checked_at` is given) AND `refreshing is False`.

    `_Result` is recorded, and `probe_count`/`checked_at` updated, inside
    the same lock the probe thread's target function returns right after
    releasing -- but the thread itself is not marked dead by the
    interpreter until a moment after that function actually returns.
    Waiting on `checked_at` alone can observe the new result while
    `refreshing` (computed from `Thread.is_alive()`) is still True; waiting
    on `refreshing` alone has the same gap in the other direction on the
    very first probe. Only waiting for both together closes it.
    """
    deadline = time.monotonic() + timeout
    snapshot = monitor.snapshot()
    while (
        (not_checked_at is not None and snapshot.checked_at == not_checked_at) or snapshot.refreshing
    ) and time.monotonic() < deadline:
        time.sleep(0.005)
        snapshot = monitor.snapshot()
    return snapshot


def test_rescan_reports_refreshing_until_the_new_result_lands() -> None:
    """A cached "available" reading stays meaningful while a fresher one is
    in flight -- the phone keeps showing what it last knew, flagged as
    being re-checked, rather than blanking out to "checking"."""
    runner = StubProbeRunner(present("SDRplay RSP1A"))
    monitor = _monitor(runner)
    try:
        first = _settled_and_not_refreshing(monitor)
        assert first.state == STATE_AVAILABLE
        assert first.refreshing is False

        gate = threading.Event()
        runner.gate = gate
        runner.entered = threading.Event()
        runner.set_outcome(present("SDRplay RSP1A (rechecked)"))

        assert monitor.request_refresh(force=True) is True
        assert runner.entered.wait(10.0), "the rescan probe never started"

        mid_flight = monitor.snapshot()
        assert mid_flight.state == STATE_AVAILABLE, "the last known state must be preserved"
        assert mid_flight.resolved_label == "SDRplay RSP1A", "showed the not-yet-landed reading"
        assert mid_flight.checked_at == first.checked_at, "checked_at moved before landing"
        assert mid_flight.refreshing is True

        gate.set()
        landed = _settled_and_not_refreshing(monitor, not_checked_at=first.checked_at)
        assert landed.checked_at != first.checked_at
        assert landed.resolved_label == "SDRplay RSP1A (rechecked)"
        assert landed.refreshing is False
    finally:
        monitor.close()


def test_ensure_fresh_waits_for_an_already_active_probe_instead_of_the_stale_cache() -> None:
    """A caller about to open the device must see whatever the in-flight
    probe reports, not race ahead with what was cached before it started."""
    runner = StubProbeRunner(present("SDRplay RSP1A"))
    monitor = _monitor(runner)
    try:
        _settled(monitor)
        assert monitor.snapshot().state == STATE_AVAILABLE

        gate = threading.Event()
        runner.gate = gate
        runner.entered = threading.Event()
        runner.set_outcome(mocked_absent)

        assert monitor.request_refresh(force=True) is True
        assert runner.entered.wait(10.0)

        result_holder: list[object] = []

        def _call() -> None:
            # A generous max_age that WOULD accept the stale "available"
            # cache if ensure_fresh took the fast path.
            result_holder.append(monitor.ensure_fresh(max_age=3600.0, wait=10.0))

        caller = threading.Thread(target=_call, daemon=True)
        caller.start()

        time.sleep(0.05)
        assert not result_holder, "ensure_fresh answered before the in-flight probe finished"

        gate.set()
        caller.join(timeout=10.0)
        assert result_holder, "ensure_fresh never returned"
        assert result_holder[0].state == STATE_DISCONNECTED, (
            "ensure_fresh answered from the stale cache instead of waiting"
        )
    finally:
        monitor.close()


# -- requirement 6: no Rescan in the readiness-to-submit transition ---------


def test_rescan_is_refused_during_the_claim_window_between_readiness_and_submit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Profile resolution sits between require_device_ready() succeeding
    and JobRegistry.submit() actually claiming the job. A concurrent Rescan
    must not be able to slip a probe into that gap."""
    import dmr_iq_surveyor.web.service as web_service

    entered_transition = threading.Event()
    release_transition = threading.Event()
    original_resolve_band_profile = web_service.resolve_band_profile

    def _slow_resolve_band_profile(*args: object, **kwargs: object):
        entered_transition.set()
        assert release_transition.wait(10.0), "the test never released the transition barrier"
        return original_resolve_band_profile(*args, **kwargs)

    monkeypatch.setattr(web_service, "resolve_band_profile", _slow_resolve_band_profile)
    monkeypatch.setattr(web_service, "run_capture", _fake_run_capture)
    monkeypatch.setattr(web_service, "run_survey", _fake_run_survey)
    monkeypatch.setattr(web_service, "materialise_measurements", _fake_materialise_measurements)

    runner = StubProbeRunner(present("SDRplay RSP1A"))
    service = _working_capture_service(tmp_path, runner)
    try:
        _settled(service.devices)
        assert runner.calls == 1

        results: list[object] = []
        worker = threading.Thread(
            target=lambda: results.append(
                service.start_capture({"duration_seconds": 1.0, "solve": False})
            ),
            daemon=True,
        )
        worker.start()
        assert entered_transition.wait(10.0), "start_capture() never reached profile resolution"

        # Deterministically inside the claim window now.
        rescan = service.rescan_device()
        assert rescan["rescan_started"] is False
        assert rescan["rescan_declined_reason"]
        assert rescan["device"]["state"] == STATE_BUSY
        assert runner.calls == 1, "a probe ran during the readiness-to-submit claim window"

        release_transition.set()
        worker.join(timeout=10.0)
        assert results, "start_capture() never returned"
        _wait_for_terminal(results[0])
    finally:
        service.close()


def test_rescan_is_refused_while_require_device_ready_is_about_to_return(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The transition lock must be held from BEFORE require_device_ready()
    is even called, not just after it succeeds -- _device_claim_count alone
    (entered only once require_device_ready() has already returned) left
    open the exact gap where the readiness check has already got its
    answer and is merely about to hand control back to start_capture()."""
    from dmr_iq_surveyor.web.service import FieldService

    entered_pause = threading.Event()
    release_pause = threading.Event()
    original_require_device_ready = FieldService.require_device_ready

    def _pausing_require_device_ready(self: FieldService) -> None:
        original_require_device_ready(self)  # the real readiness check succeeds first
        entered_pause.set()
        assert release_pause.wait(10.0), "the test never released the pause barrier"

    monkeypatch.setattr(FieldService, "require_device_ready", _pausing_require_device_ready)

    import dmr_iq_surveyor.web.service as web_service

    monkeypatch.setattr(web_service, "run_capture", _fake_run_capture)
    monkeypatch.setattr(web_service, "run_survey", _fake_run_survey)
    monkeypatch.setattr(web_service, "materialise_measurements", _fake_materialise_measurements)

    runner = StubProbeRunner(present("SDRplay RSP1A"))
    service = _working_capture_service(tmp_path, runner)
    try:
        _settled(service.devices)
        assert runner.calls == 1

        results: list[object] = []
        worker = threading.Thread(
            target=lambda: results.append(
                service.start_capture({"duration_seconds": 1.0, "solve": False})
            ),
            daemon=True,
        )
        worker.start()
        assert entered_pause.wait(10.0), "start_capture() never reached the post-readiness pause"

        # Deterministically inside the window now: the readiness check has
        # already succeeded and start_capture() has not yet even regained
        # control from require_device_ready(), let alone reached profile
        # resolution or submit().
        rescan = service.rescan_device()
        assert rescan["rescan_started"] is False
        assert rescan["rescan_declined_reason"]
        assert runner.calls == 1, "a probe ran while the transition lock was held"

        release_pause.set()
        worker.join(timeout=10.0)
        assert results, "start_capture() never returned"
        _wait_for_terminal(results[0])
    finally:
        service.close()


# -- requirement 7: a runner that raises must not wedge the monitor --------


def test_a_runner_that_raises_produces_failed_not_a_stuck_checking() -> None:
    def _raiser(driver: str) -> ProbeOutcome:
        raise RuntimeError("the runner itself is broken")

    runner = StubProbeRunner(_raiser)
    monitor = _monitor(runner)
    try:
        _wait_for_probes(monitor, 1)
        snapshot = monitor.snapshot()
        assert snapshot.state == STATE_FAILED
        assert snapshot.available is False
        assert "RuntimeError" in (snapshot.probe_error or "")
        assert snapshot.reason == REASON_RUNNER_RAISED

        # The thread that raised must not have wedged the monitor: a later,
        # working probe still lands normally rather than staying "checking"
        # (or the same failure) forever.
        runner.set_outcome(present("SDRplay RSP1A"))
        assert monitor.request_refresh(force=True) is True
        _wait_for_probes(monitor, 2)
        assert monitor.snapshot().state == STATE_AVAILABLE
    finally:
        monitor.close()
