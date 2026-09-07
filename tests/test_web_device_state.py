"""Device state the field app can always answer with.

The failure these pin: `GET /api/state` used to call `probe_soapysdr()`
inline, so a wedged SDRplay API service made every state request hang --
with no timeout, no retry and, because BaseHTTPRequestHandler only logs
from send_response(), no log line either. The phone sat on "connecting…"
and "disk…" indefinitely and no recording was ever made.

Nothing here opens an SDR or starts a probe process.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import pytest
from fixtures.device_probe import StubProbeRunner, mocked_absent, present
from fixtures.geo_scenario import Transmitter, build_database, seed_run

from dmr_iq_surveyor.capture._soapy_probe import PROBE_DISCONNECTED, PROBE_NOT_SUPPORTED
from dmr_iq_surveyor.capture.probe import (
    REASON_SPAWN_FAILED,
    STATE_AVAILABLE,
    STATE_DISCONNECTED,
    STATE_FAILED,
    STATE_NOT_SUPPORTED,
    STATE_TIMED_OUT,
    ProbeOutcome,
)
from dmr_iq_surveyor.web.devices import (
    AVAILABLE_TTL_SECONDS,
    DISCONNECTED_BACKOFF_SECONDS,
    STATE_BUSY,
    STATE_CHECKING,
    DeviceMonitor,
)
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
