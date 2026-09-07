"""Running the SDR probe where it can actually be stopped.

`probe_soapysdr()` is a blocking C++ call in the end: `Device.enumerate()`
talks to the SDRplay API service, and when that service is wedged the call
never returns. Nothing in Python can cancel it -- not a timeout, not a
thread, not an exception -- so a probe on a request thread is a request
that never answers, which is exactly how the field app hung with no log
line to show for it.

So the probe runs in a child process instead. A child can be killed. The
contract here is deliberately narrow:

* `ProbeRunner` is the seam. `FieldService` is given one; it never inspects
  it, never special-cases it, and never reaches around it to the hardware.
* `SubprocessProbeRunner` is the real one. On a spawn failure it reports
  `probe_spawn_failed` and stops -- it never falls back to probing in this
  process, because that is the hang this module exists to prevent.
* At most **one** unreaped child is ever tolerated. If a kill does not take,
  the handle is kept, a circuit breaker opens, and no further probe is
  started until that child is finally reaped. Leaking a second one would
  trade a bounded problem for an unbounded one.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from dmr_iq_surveyor.capture import _soapy_probe
from dmr_iq_surveyor.capture._soapy_probe import (
    PROBE_DISCONNECTED,
    PROBE_FAILED,
    PROBE_NOT_SUPPORTED,
)

# Probe outcome states. The field app adds two more of its own that no probe
# can report -- "checking" (none has finished yet) and "busy" (a capture
# holds the device, so none is being run) -- in `web/devices.py`.
STATE_AVAILABLE = "available"
STATE_DISCONNECTED = "disconnected"
STATE_FAILED = "failed"
STATE_NOT_SUPPORTED = "not_supported"
STATE_TIMED_OUT = "timed_out"

# Machine-readable reasons, so a caller never has to match on English.
REASON_SPAWN_FAILED = "probe_spawn_failed"
REASON_CHILD_ERROR = "probe_child_error"
REASON_BAD_OUTPUT = "probe_bad_output"
REASON_UNCLASSIFIED = "probe_unclassified"
REASON_TIMED_OUT = "probe_timed_out"
REASON_ORPHANED = "probe_orphaned"
REASON_RUNNER_CLOSED = "probe_runner_closed"

# Generous next to a healthy probe (a child that imports only the leaf
# module reaches SoapySDR in about a tenth of a second, and a working
# enumerate answers in one or two), and short enough that a wedged SDRplay
# service is reported inside one screen refresh rather than one coffee.
DEFAULT_PROBE_TIMEOUT_SECONDS = 8.0

# How long to wait for a killed child to actually die before treating it as
# an orphan. SIGKILL is immediate unless the process is stuck in an
# uninterruptible kernel call -- a USB ioctl, which is precisely the failure
# mode in question -- and waiting longer would not change the answer.
_REAP_TIMEOUT_SECONDS = 2.0

# Enough of the child's stderr to diagnose it, not enough to fill a phone.
_STDERR_EXCERPT_CHARS = 500

_REQUIRED_KEYS = frozenset(
    {"available", "requested_driver", "resolved_label", "probe_error", "devices_found"}
)

_REASON_TO_STATE = {
    PROBE_NOT_SUPPORTED: STATE_NOT_SUPPORTED,
    PROBE_DISCONNECTED: STATE_DISCONNECTED,
    PROBE_FAILED: STATE_FAILED,
}


@dataclass(frozen=True, slots=True)
class ProbeOutcome:
    """One probe attempt, whatever became of it."""

    state: str
    detail: str | None = None
    reason: str | None = None
    resolved_label: str | None = None
    devices_found: list[dict[str, str]] = field(default_factory=list)
    duration_seconds: float = 0.0

    @property
    def available(self) -> bool:
        return self.state == STATE_AVAILABLE


class ProbeRunner(Protocol):
    """How the field app asks whether an SDR is there.

    Deliberately tiny: one call that always returns within
    `timeout_seconds`, and one that releases whatever it is holding.
    """

    def run(self, driver: str, *, timeout_seconds: float) -> ProbeOutcome: ...

    def close(self) -> None: ...


def _excerpt(text: str | None) -> str:
    collapsed = " ".join((text or "").split())
    if len(collapsed) <= _STDERR_EXCERPT_CHARS:
        return collapsed
    return collapsed[:_STDERR_EXCERPT_CHARS] + "…"


def _last_json_object(stdout: str) -> dict[str, Any] | None:
    """The last JSON object on stdout, ignoring anything else printed there.

    SoapySDR and the SDRplay module write their own chatter to the same
    stream from C++, before and after ours, so the child's answer is found
    rather than assumed to be the whole output.
    """
    for line in reversed(stdout.splitlines()):
        candidate = line.strip()
        if not candidate.startswith("{"):
            continue
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _release(process: subprocess.Popen[str]) -> None:
    """Close a finished child's pipes without caring how that goes."""
    for stream in (process.stdout, process.stderr, process.stdin):
        if stream is None:
            continue
        try:
            stream.close()
        except OSError:
            pass


class SubprocessProbeRunner:
    """Runs the probe in a killable child process.

    One instance is shared by the whole field app and is expected to be
    called by a single caller at a time (`DeviceMonitor` guarantees that);
    the lock here protects the handles against `close()` arriving from
    another thread mid-probe.
    """

    def __init__(
        self,
        *,
        executable: str | None = None,
        script: str | Path | None = None,
    ) -> None:
        self._executable = executable or sys.executable
        self._script = str(Path(script) if script is not None else Path(_soapy_probe.__file__))
        self._lock = threading.Lock()
        self._current: subprocess.Popen[str] | None = None
        self._orphan: subprocess.Popen[str] | None = None
        self._closed = False

    # -- state a caller may want to see ------------------------------------

    @property
    def orphaned(self) -> bool:
        """True while a killed child has still not been reaped."""
        with self._lock:
            return self._orphan is not None

    # -- the seam ----------------------------------------------------------

    def run(self, driver: str, *, timeout_seconds: float) -> ProbeOutcome:
        started = time.monotonic()

        blocked = self._orphan_verdict(started)
        if blocked is not None:
            return blocked

        with self._lock:
            if self._closed:
                return ProbeOutcome(
                    state=STATE_FAILED,
                    reason=REASON_RUNNER_CLOSED,
                    detail="the field app is shutting down, so no SDR probe was started",
                    duration_seconds=time.monotonic() - started,
                )

        try:
            process = subprocess.Popen(  # noqa: S603 - fixed interpreter and script, no shell
                [self._executable, self._script, driver],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        except OSError as exc:
            # No in-process retry. Falling back to probe_soapysdr() here is
            # what would reintroduce the unkillable hang this class exists
            # to prevent, so a spawn failure is simply reported as one.
            return ProbeOutcome(
                state=STATE_FAILED,
                reason=REASON_SPAWN_FAILED,
                detail=(
                    f"could not start the SDR probe process ({type(exc).__name__}: {exc}). "
                    f"Interpreter: {self._executable!r}; probe: {self._script!r}."
                ),
                duration_seconds=time.monotonic() - started,
            )

        with self._lock:
            self._current = process
        try:
            stdout, stderr = process.communicate(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            return self._kill(process, timeout_seconds, started)
        finally:
            with self._lock:
                if self._current is process:
                    self._current = None

        return self._interpret(
            stdout=stdout,
            stderr=stderr,
            returncode=process.returncode,
            duration_seconds=time.monotonic() - started,
        )

    def close(self) -> None:
        """Stop probing and let go of any child.

        A probe that is in flight is killed but deliberately **not** waited
        on here: the thread inside `run()` is already blocked in that
        child's `communicate()`, and a second concurrent `communicate()` on
        the same handle tears the pipes out from under it. Killing is
        enough -- the blocked call returns on its own, and `run()` does its
        own cleanup. Only the orphan, which no thread is waiting on, is
        reaped from here.
        """
        with self._lock:
            self._closed = True
            current = self._current
            orphan = self._orphan
        if current is not None:
            try:
                current.kill()
            except OSError:
                pass
        if orphan is not None:
            try:
                orphan.kill()
            except OSError:
                pass
            try:
                orphan.communicate(timeout=_REAP_TIMEOUT_SECONDS)
            except (subprocess.TimeoutExpired, OSError, ValueError):
                _release(orphan)
            with self._lock:
                if self._orphan is orphan and orphan.poll() is not None:
                    self._orphan = None

    # -- internals ---------------------------------------------------------

    def _orphan_verdict(self, started: float) -> ProbeOutcome | None:
        """Refuse to start a probe while a previous one is still unreaped.

        Every attempt first re-checks the orphan, so the breaker closes by
        itself the moment the kernel finally lets that process go -- no
        restart needed, and no second stuck process created in the meantime.
        """
        with self._lock:
            orphan = self._orphan
        if orphan is None:
            return None
        if orphan.poll() is None:
            return ProbeOutcome(
                state=STATE_TIMED_OUT,
                reason=REASON_ORPHANED,
                detail=(
                    "an earlier SDR check is still stuck and could not be stopped, so no new "
                    "check was started. The SDRplay API service needs attention (on the Pi: "
                    "`systemctl status sdrplay`); restarting the field service also clears this."
                ),
                duration_seconds=time.monotonic() - started,
            )
        _release(orphan)
        with self._lock:
            if self._orphan is orphan:
                self._orphan = None
        return None

    def _kill(
        self,
        process: subprocess.Popen[str],
        timeout_seconds: float,
        started: float,
    ) -> ProbeOutcome:
        try:
            process.kill()
        except OSError:
            pass
        try:
            process.communicate(timeout=_REAP_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            with self._lock:
                self._orphan = process
            return ProbeOutcome(
                state=STATE_TIMED_OUT,
                reason=REASON_ORPHANED,
                detail=(
                    f"the SDR check did not finish within {timeout_seconds:g} s and could not "
                    "be stopped. Until it exits, no further check will be started."
                ),
                duration_seconds=time.monotonic() - started,
            )
        return ProbeOutcome(
            state=STATE_TIMED_OUT,
            reason=REASON_TIMED_OUT,
            detail=(
                f"the SDR check did not finish within {timeout_seconds:g} s and was stopped. "
                "The SDRplay API service may be wedged; use Rescan SDR to try again."
            ),
            duration_seconds=time.monotonic() - started,
        )

    def _interpret(
        self,
        *,
        stdout: str,
        stderr: str,
        returncode: int | None,
        duration_seconds: float,
    ) -> ProbeOutcome:
        if returncode != 0:
            return ProbeOutcome(
                state=STATE_FAILED,
                reason=REASON_CHILD_ERROR,
                detail=(
                    f"the SDR probe process exited with code {returncode}. "
                    f"Output: {_excerpt(stderr) or '(none)'}"
                ),
                duration_seconds=duration_seconds,
            )

        payload = _last_json_object(stdout)
        if payload is None or not _REQUIRED_KEYS.issubset(payload):
            return ProbeOutcome(
                state=STATE_FAILED,
                reason=REASON_BAD_OUTPUT,
                detail=(
                    "the SDR probe process did not report a usable result. "
                    f"Output: {_excerpt(stdout) or '(none)'}"
                ),
                duration_seconds=duration_seconds,
            )

        if payload["available"]:
            devices = [dict(entry) for entry in payload["devices_found"]]
            return ProbeOutcome(
                state=STATE_AVAILABLE,
                resolved_label=payload["resolved_label"],
                devices_found=devices,
                duration_seconds=duration_seconds,
            )

        state = _REASON_TO_STATE.get(payload.get("reason"))
        if state is None:
            return ProbeOutcome(
                state=STATE_FAILED,
                reason=REASON_UNCLASSIFIED,
                detail=(
                    "the SDR probe reported no device without saying why. "
                    f"Reported: {_excerpt(payload.get('probe_error')) or '(no detail)'}"
                ),
                duration_seconds=duration_seconds,
            )
        return ProbeOutcome(
            state=state,
            reason=payload.get("reason"),
            detail=payload["probe_error"],
            duration_seconds=duration_seconds,
        )


__all__ = [
    "DEFAULT_PROBE_TIMEOUT_SECONDS",
    "REASON_BAD_OUTPUT",
    "REASON_CHILD_ERROR",
    "REASON_ORPHANED",
    "REASON_RUNNER_CLOSED",
    "REASON_SPAWN_FAILED",
    "REASON_TIMED_OUT",
    "REASON_UNCLASSIFIED",
    "STATE_AVAILABLE",
    "STATE_DISCONNECTED",
    "STATE_FAILED",
    "STATE_NOT_SUPPORTED",
    "STATE_TIMED_OUT",
    "ProbeOutcome",
    "ProbeRunner",
    "SubprocessProbeRunner",
]
