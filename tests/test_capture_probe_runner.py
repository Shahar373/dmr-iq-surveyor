"""The out-of-process SDR probe: every way a child process can let us down.

None of these tests opens an SDR, and none of them depends on whether
SoapySDR is installed on the machine running the suite -- each one supplies
its own child process (or refuses to start one at all).
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from dmr_iq_surveyor.capture import probe as probe_module
from dmr_iq_surveyor.capture._soapy_probe import (
    PROBE_DISCONNECTED,
    PROBE_NOT_SUPPORTED,
    probe_payload,
)
from dmr_iq_surveyor.capture.probe import (
    REASON_BAD_OUTPUT,
    REASON_CHILD_ERROR,
    REASON_ORPHANED,
    REASON_RUNNER_CLOSED,
    REASON_SPAWN_FAILED,
    REASON_TIMED_OUT,
    REASON_UNCLASSIFIED,
    STATE_AVAILABLE,
    STATE_DISCONNECTED,
    STATE_FAILED,
    STATE_NOT_SUPPORTED,
    STATE_TIMED_OUT,
    SubprocessProbeRunner,
)

GOOD_PAYLOAD = {
    "available": True,
    "requested_driver": "sdrplay",
    "resolved_label": "SDRplay RSP1A 230405A498",
    "probe_error": None,
    "devices_found": [{"driver": "sdrplay", "label": "SDRplay RSP1A 230405A498"}],
    "reason": None,
}


def _child(tmp_path: Path, body: str) -> SubprocessProbeRunner:
    """A runner whose child is the given script instead of the real probe."""
    script = tmp_path / "fake_probe.py"
    script.write_text(body, encoding="utf-8")
    return SubprocessProbeRunner(script=script)


def test_a_child_that_exits_nonzero_is_reported_as_a_child_error(tmp_path: Path) -> None:
    runner = _child(
        tmp_path,
        "import sys\nsys.stderr.write('libsdrplay: cannot open device\\n')\nsys.exit(3)\n",
    )
    outcome = runner.run("sdrplay", timeout_seconds=10.0)
    assert outcome.state == STATE_FAILED
    assert outcome.reason == REASON_CHILD_ERROR
    assert "exited with code 3" in outcome.detail
    assert "cannot open device" in outcome.detail
    assert outcome.available is False


def test_malformed_json_is_not_read_as_a_device(tmp_path: Path) -> None:
    runner = _child(tmp_path, "print('{\"available\": tru')\n")
    outcome = runner.run("sdrplay", timeout_seconds=10.0)
    assert outcome.state == STATE_FAILED
    assert outcome.reason == REASON_BAD_OUTPUT


def test_a_silent_child_is_not_read_as_a_device(tmp_path: Path) -> None:
    runner = _child(tmp_path, "pass\n")
    outcome = runner.run("sdrplay", timeout_seconds=10.0)
    assert outcome.state == STATE_FAILED
    assert outcome.reason == REASON_BAD_OUTPUT


def test_valid_json_missing_fields_is_rejected(tmp_path: Path) -> None:
    """A partial payload must not be padded with guesses: an absent
    `devices_found` is not an empty one, and an absent `available` is not
    False. The probe says so instead."""
    runner = _child(tmp_path, "import json\nprint(json.dumps({'available': True}))\n")
    outcome = runner.run("sdrplay", timeout_seconds=10.0)
    assert outcome.state == STATE_FAILED
    assert outcome.reason == REASON_BAD_OUTPUT


def test_the_answer_is_found_among_the_drivers_own_chatter(tmp_path: Path) -> None:
    """libSoapySDR and SoapySDRPlay3 write to the same stdout from C++.
    Their lines, before and after ours, must not cost us the result."""
    runner = _child(
        tmp_path,
        "import json\n"
        "print('[INFO] SoapySDR::loadModule')\n"
        f"print(json.dumps({GOOD_PAYLOAD!r}))\n"
        "print('[INFO] devices released')\n",
    )
    outcome = runner.run("sdrplay", timeout_seconds=10.0)
    assert outcome.state == STATE_AVAILABLE
    assert outcome.resolved_label == "SDRplay RSP1A 230405A498"
    assert outcome.devices_found == GOOD_PAYLOAD["devices_found"]


def test_stderr_noise_does_not_spoil_a_good_result(tmp_path: Path) -> None:
    runner = _child(
        tmp_path,
        "import json, sys\n"
        "sys.stderr.write('sdrplay_api: warning\\n')\n"
        f"print(json.dumps({GOOD_PAYLOAD!r}))\n",
    )
    outcome = runner.run("sdrplay", timeout_seconds=10.0)
    assert outcome.state == STATE_AVAILABLE
    assert outcome.reason is None


def test_an_unclassified_absence_is_reported_as_such(tmp_path: Path) -> None:
    """"No device, and no reason given" is its own answer -- not silently
    filed as "unplugged"."""
    payload = dict(GOOD_PAYLOAD, available=False, resolved_label=None, reason=None)
    runner = _child(tmp_path, f"import json\nprint(json.dumps({payload!r}))\n")
    outcome = runner.run("sdrplay", timeout_seconds=10.0)
    assert outcome.state == STATE_FAILED
    assert outcome.reason == REASON_UNCLASSIFIED


def test_a_child_reporting_missing_bindings_is_not_supported(tmp_path: Path) -> None:
    payload = {
        "available": False,
        "requested_driver": "sdrplay",
        "resolved_label": None,
        "probe_error": "SoapySDR Python bindings are not importable (ImportError: no).",
        "devices_found": [],
        "reason": PROBE_NOT_SUPPORTED,
    }
    runner = _child(tmp_path, f"import json\nprint(json.dumps({payload!r}))\n")
    outcome = runner.run("sdrplay", timeout_seconds=10.0)
    assert outcome.state == STATE_NOT_SUPPORTED
    assert "not importable" in outcome.detail


def test_a_child_reporting_no_matching_device_is_disconnected(tmp_path: Path) -> None:
    payload = {
        "available": False,
        "requested_driver": "sdrplay",
        "resolved_label": None,
        "probe_error": "No SoapySDR device matched driver='sdrplay'.",
        "devices_found": [],
        "reason": PROBE_DISCONNECTED,
    }
    runner = _child(tmp_path, f"import json\nprint(json.dumps({payload!r}))\n")
    outcome = runner.run("sdrplay", timeout_seconds=10.0)
    assert outcome.state == STATE_DISCONNECTED
    assert outcome.available is False


def test_the_probe_itself_classifies_absent_bindings(monkeypatch: pytest.MonkeyPatch) -> None:
    """The child's half of the same contract, exercised in-process so it is
    deterministic whether or not SoapySDR is installed here. Setting a
    module to None in sys.modules forces the next `import` to raise."""
    monkeypatch.setitem(sys.modules, "SoapySDR", None)
    payload = probe_payload("sdrplay")
    assert payload["available"] is False
    assert payload["reason"] == PROBE_NOT_SUPPORTED
    assert set(payload) == {
        "available",
        "requested_driver",
        "resolved_label",
        "probe_error",
        "devices_found",
        "reason",
    }
    # The child prints exactly this, and the parent must read it back.
    assert json.loads(json.dumps(payload)) == payload


def test_a_spawn_failure_is_reported_and_never_probed_in_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Falling back to an in-process probe here would reintroduce the
    unkillable hang the child process exists to prevent."""

    def _must_not_run(*args: object, **kwargs: object) -> None:
        raise AssertionError("the runner probed in this process after a spawn failure")

    monkeypatch.setattr("dmr_iq_surveyor.capture.device.probe_soapysdr", _must_not_run)
    monkeypatch.setattr("dmr_iq_surveyor.capture._soapy_probe.probe_payload", _must_not_run)

    runner = SubprocessProbeRunner(executable="/nonexistent/interpreter")
    outcome = runner.run("sdrplay", timeout_seconds=10.0)
    assert outcome.state == STATE_FAILED
    assert outcome.reason == REASON_SPAWN_FAILED
    assert "/nonexistent/interpreter" in outcome.detail


def test_a_child_that_overruns_is_killed_and_reported(tmp_path: Path) -> None:
    runner = _child(tmp_path, "import time\ntime.sleep(30)\n")
    started = time.monotonic()
    outcome = runner.run("sdrplay", timeout_seconds=0.5)
    elapsed = time.monotonic() - started
    assert outcome.state == STATE_TIMED_OUT
    assert outcome.reason == REASON_TIMED_OUT
    assert elapsed < 10.0
    assert runner.orphaned is False


class _StuckPopen:
    """A child that ignores kill(), the way a process wedged in a USB ioctl
    does. `revive()` is the kernel finally letting it go."""

    def __init__(self) -> None:
        self.stdout = None
        self.stderr = None
        self.stdin = None
        self.returncode: int | None = None
        self.kills = 0

    def communicate(self, timeout: float | None = None) -> tuple[str, str]:
        if self.returncode is None:
            raise subprocess.TimeoutExpired(cmd="fake", timeout=timeout or 0.0)
        return "", ""

    def kill(self) -> None:
        self.kills += 1

    def poll(self) -> int | None:
        return self.returncode

    def revive(self) -> None:
        self.returncode = -9


def test_a_child_that_cannot_be_reaped_opens_a_breaker_and_closes_it_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """At most one unreaped child, ever. A second probe is refused rather
    than started, and the refusal lifts by itself once the first one goes."""
    stuck = _StuckPopen()
    spawned: list[_StuckPopen] = []

    def _popen(*args: object, **kwargs: object) -> _StuckPopen:
        spawned.append(stuck)
        return stuck

    monkeypatch.setattr(probe_module.subprocess, "Popen", _popen)
    runner = SubprocessProbeRunner()

    first = runner.run("sdrplay", timeout_seconds=0.1)
    assert first.state == STATE_TIMED_OUT
    assert first.reason == REASON_ORPHANED
    assert stuck.kills == 1
    assert runner.orphaned is True
    assert len(spawned) == 1

    second = runner.run("sdrplay", timeout_seconds=0.1)
    assert second.reason == REASON_ORPHANED
    assert len(spawned) == 1, "a second child was started while one was still stuck"

    stuck.revive()
    third = runner.run("sdrplay", timeout_seconds=0.1)
    assert runner.orphaned is False
    assert len(spawned) == 2, "the breaker did not close once the child was reaped"
    assert third.reason != REASON_ORPHANED, "still refusing to probe after the child was reaped"


def test_closing_the_runner_during_a_probe_kills_the_child(tmp_path: Path) -> None:
    runner = _child(tmp_path, "import time\ntime.sleep(60)\n")
    outcomes: list[object] = []

    worker = threading.Thread(
        target=lambda: outcomes.append(runner.run("sdrplay", timeout_seconds=60.0)),
        daemon=True,
    )
    worker.start()
    deadline = time.monotonic() + 10.0
    while runner._current is None and time.monotonic() < deadline:  # noqa: SLF001 - own internals
        time.sleep(0.01)
    child = runner._current  # noqa: SLF001 - asserting the process really goes away
    assert child is not None

    runner.close()
    worker.join(timeout=10.0)
    assert not worker.is_alive(), "run() did not return after the runner was closed"
    assert child.poll() is not None, "the child process outlived the runner"
    assert outcomes, "run() returned nothing"



class _BarrierPopen:
    """A fake, ordinarily killable child -- used to prove close() kills a
    process spawned mid-close(), not to model an unkillable one (see
    test_a_child_that_cannot_be_reaped_opens_a_breaker_and_closes_it_again
    for that)."""

    def __init__(self) -> None:
        self.stdout = None
        self.stderr = None
        self.stdin = None
        self.returncode: int | None = None
        self.killed = False

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    def communicate(self, timeout: float | None = None) -> tuple[str, str]:
        if self.returncode is None:
            raise subprocess.TimeoutExpired(cmd="fake", timeout=timeout or 0.0)
        return "", ""

    def poll(self) -> int | None:
        return self.returncode


def test_close_during_spawn_kills_the_new_child_rather_than_orphaning_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Popen() is not instantaneous, and nothing holds the runner's lock
    across it. If close() lands in that exact window -- after run() already
    passed its own closed-check, before the spawned process is registered
    into self._current -- close() sees nothing to kill. A barrier around
    the fake Popen() forces that window deterministically, every run,
    instead of hoping timing lines up."""
    spawn_entered = threading.Event()
    release_spawn = threading.Event()
    spawned = _BarrierPopen()

    def _slow_popen(*_args: object, **_kwargs: object) -> _BarrierPopen:
        spawn_entered.set()
        assert release_spawn.wait(5.0), "the test never released the spawn barrier"
        return spawned

    monkeypatch.setattr(probe_module.subprocess, "Popen", _slow_popen)
    runner = SubprocessProbeRunner()

    outcomes: list[object] = []
    worker = threading.Thread(
        target=lambda: outcomes.append(runner.run("sdrplay", timeout_seconds=5.0)),
        daemon=True,
    )
    worker.start()

    assert spawn_entered.wait(5.0), "Popen() was never called"
    # Deterministically inside the race window now: Popen() has been
    # called and has not yet returned to run().
    runner.close()
    release_spawn.set()
    worker.join(timeout=5.0)

    assert not worker.is_alive(), "run() did not return after close()"
    assert spawned.killed, "a child spawned during close() was left unowned"
    assert outcomes, "run() returned nothing"
    outcome = outcomes[0]
    assert outcome.state == STATE_FAILED
    assert outcome.reason == REASON_RUNNER_CLOSED
