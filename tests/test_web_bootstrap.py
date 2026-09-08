"""Static and behavioural contract checks between app.js and the server.

These pin the frontend side of the same field failure `test_web_device_state.py`
covers on the backend: a stuck "connecting…"/"disk…" with nothing on screen to
explain it. Three things made that possible and are locked down here --

* an element id referenced from app.js that does not exist in index.html
  (`wireUi()` throws on the very first `addEventListener`, before anything
  else runs);
* a key `refreshState()` reads from the `/api/state` payload that some
  device state does not actually carry, so rendering throws mid-state;
* `wireUi()` running outside `boot()`'s own `try`, or `api()` lacking any
  timeout/cancellation, either of which turns a slow or failed request back
  into the original unbounded hang.

Nothing here starts a browser: app.js is parsed as text, and the payload
checks go straight through `FieldService.state()` -- exactly what
`GET /api/state` serialises, see `web/server.py`'s `do_GET`.
"""

from __future__ import annotations

import re
import threading
from pathlib import Path

from fixtures.device_probe import StubProbeRunner, mocked_absent, present
from fixtures.geo_scenario import build_database

from dmr_iq_surveyor.capture._soapy_probe import PROBE_NOT_SUPPORTED
from dmr_iq_surveyor.capture.probe import (
    REASON_SPAWN_FAILED,
    STATE_FAILED,
    STATE_NOT_SUPPORTED,
    STATE_TIMED_OUT,
    ProbeOutcome,
)
from dmr_iq_surveyor.web.devices import STATE_BUSY, STATE_CHECKING
from dmr_iq_surveyor.web.server import STATIC_ROOT
from dmr_iq_surveyor.web.service import FieldService, FieldSettings

APP_JS = (STATIC_ROOT / "app.js").read_text()
INDEX_HTML = (STATIC_ROOT / "index.html").read_text()


def _settled(service: FieldService, timeout: float = 10.0) -> None:
    import time

    deadline = time.monotonic() + timeout
    while service.devices.probe_count == 0 and time.monotonic() < deadline:
        time.sleep(0.005)
    assert service.devices.probe_count >= 1, "the initial probe never completed"


def _service(tmp_path: Path, runner: StubProbeRunner) -> FieldService:
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


# -- 31: every element id app.js looks up exists in index.html --------------


def _html_ids() -> set[str]:
    return set(re.findall(r'\bid="([\w-]+)"', INDEX_HTML))


def _js_referenced_ids() -> set[str]:
    """IDs app.js looks up by a literal `$("#id")`/`$(id)`/`"#" + id` call.

    Covers every referencing shape actually present in the file: a plain
    `$("#foo")`, the KML/GPX export loop's `$(id)` over a literal
    `["#export-kml", ...]` array, and the layer-toggle loop's `$("#" + id)`
    over the string keys of the `toggles` object.
    """
    ids = set(re.findall(r'\$\("#([\w-]+)"\)', APP_JS))

    export_array = re.search(r"for \(const \[id, format\] of (\[.*?\])\)", APP_JS)
    assert export_array, "the export-id loop in wireUi() was not found as expected"
    ids.update(m.lstrip("#") for m in re.findall(r'"(#[\w-]+)"', export_array.group(1)))

    toggles_block = re.search(r"const toggles = \{(.*?)\};", APP_JS, re.DOTALL)
    assert toggles_block, "the layer-toggle map in wireUi() was not found as expected"
    ids.update(re.findall(r'"([\w-]+)":', toggles_block.group(1)))

    return ids


def test_every_id_app_js_looks_up_exists_in_index_html() -> None:
    referenced = _js_referenced_ids()
    assert referenced, "the id-extraction regexes matched nothing -- they no longer fit app.js"
    missing = referenced - _html_ids()
    assert not missing, (
        f"app.js looks up {sorted(missing)}, which index.html does not define -- "
        "wireUi() would throw on the first missing one and boot() would show a "
        "fatal-error banner instead of the app"
    )


# -- 32: every key refreshState() reads exists in /api/state, in every state -


def _refresh_state_body() -> str:
    match = re.search(r"async function refreshState\(\) \{(.*?)\n\}\n", APP_JS, re.DOTALL)
    assert match, "refreshState() was not found in app.js as expected"
    return match.group(1)


def _refresh_state_payload_keys() -> set[str]:
    keys = set(re.findall(r"\bpayload\.(\w+)\b", _refresh_state_body()))
    assert keys, "the payload.<key> regex matched nothing -- refreshState() changed shape"
    return keys


def test_refresh_state_reads_only_keys_the_payload_actually_has(tmp_path: Path) -> None:
    keys = _refresh_state_payload_keys()

    cases: dict[str, StubProbeRunner] = {
        "disconnected": StubProbeRunner(mocked_absent),
        "available": StubProbeRunner(present("SDRplay RSP1A")),
        "failed": StubProbeRunner(
            ProbeOutcome(
                state=STATE_FAILED,
                reason=REASON_SPAWN_FAILED,
                detail="could not start the SDR probe process (OSError: nope).",
            )
        ),
        "not_supported": StubProbeRunner(
            ProbeOutcome(
                state=STATE_NOT_SUPPORTED,
                reason=PROBE_NOT_SUPPORTED,
                detail="SoapySDR Python bindings are not importable.",
            )
        ),
        "timed_out": StubProbeRunner(
            ProbeOutcome(state=STATE_TIMED_OUT, detail="the SDR check did not finish in 8 s.")
        ),
    }

    for label, runner in cases.items():
        service = _service(tmp_path / label, runner)
        try:
            _settled(service)
            payload = service.state()
            assert payload["device"]["state"] != STATE_CHECKING, label
            missing = keys - payload.keys()
            assert not missing, f"[{label}] /api/state is missing {sorted(missing)}"
        finally:
            service.close()

    # checking: sampled before the constructor's own background probe lands.
    checking_service = _service(tmp_path / "checking", StubProbeRunner(gate=threading.Event()))
    try:
        payload = checking_service.state()
        assert payload["device"]["state"] == STATE_CHECKING
        missing = keys - payload.keys()
        assert not missing, f"[checking] /api/state is missing {sorted(missing)}"
    finally:
        checking_service.close()

    # busy: a job that holds the device, exactly as a real capture would.
    busy_service = _service(tmp_path / "busy", StubProbeRunner(present("SDRplay RSP1A")))
    try:
        _settled(busy_service)
        release = threading.Event()
        job = busy_service.jobs.submit(
            kind="capture", label="fake capture", work=lambda job: (release.wait(10.0), {})[1]
        )
        try:
            payload = busy_service.state()
            assert payload["device"]["state"] == STATE_BUSY
            missing = keys - payload.keys()
            assert not missing, f"[busy] /api/state is missing {sorted(missing)}"
        finally:
            release.set()
            deadline_job = job
            import time as _time

            deadline = _time.monotonic() + 10.0
            while not deadline_job.is_terminal() and _time.monotonic() < deadline:
                _time.sleep(0.005)
    finally:
        busy_service.close()


# -- 33: api() has a real timeout, and boot() wraps wireUi() too ------------


def test_api_helper_has_a_cancellation_and_timeout_mechanism() -> None:
    match = re.search(r"async function api\(path, options = \{\}\) \{(.*?)\n\}\n", APP_JS, re.DOTALL)
    assert match, "api() was not found in app.js as expected"
    body = match.group(1)
    assert "AbortController" in body, "api() no longer aborts the request on timeout"
    assert "setTimeout" in body and "controller.abort()" in body, (
        "api() no longer schedules a timeout that aborts the fetch"
    )
    assert "signal: controller.signal" in body or "signal:controller.signal" in body, (
        "api() builds an AbortController but never wires its signal into fetch()"
    )


def test_boot_wraps_wire_ui_in_its_own_try_block() -> None:
    match = re.search(r"async function boot\(\) \{(.*?)\n\}\n\nboot\(\);", APP_JS, re.DOTALL)
    assert match, "boot() was not found in app.js as expected"
    body = match.group(1)
    assert re.search(r"try \{\s*wireUi\(\);", body), (
        "wireUi() must be the first call inside boot()'s own try -- a throw in "
        "wireUi() must show the fatal-error banner, not escape as an unhandled "
        "rejection with nothing on screen"
    )
    assert "except" not in body  # sanity: this is JS, not stray Python


def test_a_fatal_boot_error_is_shown_rather_than_a_silent_hang() -> None:
    """`boot()`'s own catch, and the window-level error/unhandledrejection
    handlers, must all end up calling the same visible-banner function --
    the specific gap that once left a stuck "connecting…" with no trace."""
    assert "function showFatalError(message)" in APP_JS
    assert 'window.addEventListener("error"' in APP_JS
    assert 'window.addEventListener("unhandledrejection"' in APP_JS
    assert "reportFatalErrorOnce" in APP_JS


def test_a_fatal_boot_error_offers_a_way_back() -> None:
    """A red banner with no recourse is not much better than the silent
    hang it replaced -- there must be a visible retry."""
    match = re.search(r"function showFatalError\(message\) \{(.*?)\n\}\n", APP_JS, re.DOTALL)
    assert match, "showFatalError() was not found in app.js as expected"
    body = match.group(1)
    assert "Try again" in body
    assert "location.reload()" in body


# -- SDR disconnects must appear without a manual action --------------------


def _function_body(name: str, *, async_fn: bool = True) -> str:
    prefix = "async function" if async_fn else "function"
    match = re.search(rf"{prefix} {re.escape(name)}\([^)]*\) \{{(.*?)\n\}}\n", APP_JS, re.DOTALL)
    assert match, f"{name}() was not found in app.js as expected"
    return match.group(1)


def test_a_repeating_poll_keeps_the_device_pill_current_without_user_action() -> None:
    """refreshState() only ever runs once, at boot, plus whenever the
    operator does something -- nothing made a disconnected SDR show up on
    its own. pollDeviceState() must reschedule itself exactly once per
    tick (one loop, not a new timer stacked on top of the last one each
    time) and must never fire while a Rescan's own polling is already
    hitting the same endpoint."""
    body = _function_body("pollDeviceState")
    assert "/api/state" in body
    assert re.search(r"devicePollTimer\s*=\s*setTimeout\(pollDeviceState,", body), (
        "pollDeviceState() must reschedule itself -- otherwise a disconnect "
        "is only ever noticed once, at boot"
    )
    assert "clearTimeout(devicePollTimer)" in body, (
        "must clear any previous timer before scheduling a new one, or two "
        "overlapping poll loops can end up running at once"
    )
    assert "rescanInFlight" in body, (
        "must skip its own fetch while a Rescan is already polling /api/state, "
        "or the two loops can race each other"
    )

    boot_body = _function_body("boot")
    assert "pollDeviceState()" in boot_body, "boot() never starts the repeating poll"


def test_pollLive_retries_on_a_dropped_link_instead_of_ending_the_drive() -> None:
    """The comment already says a dropped link is not a reason to stop
    polling -- but the code must actually retry rather than tearing down
    live.jobId and the wake lock on the very first failed poll, which
    would silently end a drive that is still running on the Pi."""
    body = _function_body("pollLive")
    assert "let ok = false;" in body and "ok = true;" in body, (
        "pollLive() must track whether the poll actually got a response, not "
        "just whether `payload` is truthy"
    )

    # Anchored to the exact, known-fixed condition (rather than a generic
    # `if (...) {` search) on purpose: a lazy `.*?` between arbitrary parens
    # can walk straight past the intended `if` into an unrelated `) {` later
    # in the function (e.g. `catch (_) {`), matching the wrong block
    # entirely -- this caught that on the first version of this test.
    match = re.search(r"if \(ok && !payload\.running\) \{(.*?)\n  \}", body, re.DOTALL)
    assert match, (
        "the teardown branch (ending the drive) must be gated on a successful "
        "response, `ok && !payload.running` -- not just a falsy/unset payload, "
        "which a caught network error produces exactly like a real 'drive "
        "stopped' would"
    )
    teardown_body = match.group(1)
    assert "live.jobId = null" in teardown_body and "releaseScreen()" in teardown_body

    after_teardown = body[body.index(match.group(0)) + len(match.group(0)) :]
    assert re.search(r"live\.timer\s*=\s*setTimeout\(pollLive,", after_teardown), (
        "there must be an unconditional retry reachable on the failure path, "
        "not only inside the success branch"
    )


def test_rescan_bounds_each_state_call_by_its_own_remaining_budget() -> None:
    """The loop deadline alone does not bound total wall-clock time: a
    single /api/state call inside it can itself take up to its own
    timeout, so a fixed per-call timeout let one Rescan run for close to
    twice its intended budget. Each call must be capped by whatever of the
    budget is actually left."""
    body = _function_body("rescanDevice")
    assert re.search(r"timeoutMs:\s*Math\.min\(STATE_TIMEOUT_MS,\s*remaining\)", body), (
        "each /api/state call inside the Rescan loop must be capped to "
        "min(STATE_TIMEOUT_MS, time remaining until the deadline), not a "
        "fixed timeout independent of how much budget is left"
    )
    assert "remaining <= 0" in body or "remaining<=0" in body, (
        "the loop must stop once the budget is exhausted rather than making "
        "one more full-timeout call past the deadline"
    )
