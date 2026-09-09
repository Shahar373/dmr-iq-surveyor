"""What the field app knows about the SDR, and when it last actually looked.

The rule this module exists to enforce: **reading device state never
touches the device**. `GET /api/state` used to call `probe_soapysdr()`
inline, so a wedged SDRplay API service turned every state request into a
request that never answered -- and because `BaseHTTPRequestHandler` only
logs from `send_response()`, it did so without leaving a single line in the
log. The phone sat on "connecting…" with nothing to show for it.

So the probe runs on its own, at most one at a time, in a child process
that can be killed (`capture/probe.py`), and every reader gets whatever the
last completed probe said -- together with when it said it. A cached
"available" is never presented as a fresh one: `checked_at`, `age_seconds`
and `stale` travel with it, always.

Refreshing is lazy, not periodic. One probe is started when the service
comes up, and after that a probe only happens because somebody read the
state and the previous answer had aged out of its window. The windows
differ by what the last answer was, because the answers differ in how
likely they are to change on their own:

* `available` holds for a long time. A device that is present does not
  leave by itself, and the two paths that genuinely depend on it -- a
  capture and a drive -- force their own fresh check anyway.
* `disconnected` retries quickly at first: this is the one state a person
  changes by hand, by plugging the cable back in, and they are standing
  there waiting for the app to notice.
* `failed` retries slowly. A repeating fault does not fix itself.
* `not_supported` retries very slowly. Missing bindings are an install
  problem; no amount of waiting resolves it.
* `timed_out` does not retry at all. Another attempt risks a second stuck
  child, and the runner refuses to start one anyway -- "Rescan SDR" is the
  deliberate way back.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from dmr_iq_surveyor.capture.probe import (
    DEFAULT_PROBE_TIMEOUT_SECONDS,
    REASON_RUNNER_RAISED,
    STATE_AVAILABLE,
    STATE_DISCONNECTED,
    STATE_FAILED,
    STATE_NOT_SUPPORTED,
    STATE_TIMED_OUT,
    ProbeOutcome,
    ProbeRunner,
)

# Two states no probe can report, because they are about this process rather
# than about the hardware.
STATE_CHECKING = "checking"
STATE_BUSY = "busy"

# How long each answer stays good for. See the module docstring for why they
# differ; the backoff tuples are indexed by how many times in a row the same
# state has come back, and hold at the last entry.
AVAILABLE_TTL_SECONDS = 300.0
NOT_SUPPORTED_TTL_SECONDS = 600.0
DISCONNECTED_BACKOFF_SECONDS = (15.0, 30.0, 60.0, 120.0)
FAILED_BACKOFF_SECONDS = (30.0, 60.0, 300.0)

_CHECKING_DETAIL = "the SDR has not been checked yet; a check is running"
_BUSY_DETAIL = (
    "a recording is running and holds the SDR, so it is not being re-checked; "
    "the reading below is from before it started"
)


def _window(state: str, repeats: int) -> float | None:
    """How long an answer of `state` stays good for. None means "forever,
    until something explicitly asks for a rescan"."""
    if state == STATE_AVAILABLE:
        return AVAILABLE_TTL_SECONDS
    if state == STATE_NOT_SUPPORTED:
        return NOT_SUPPORTED_TTL_SECONDS
    if state == STATE_TIMED_OUT:
        return None
    table = DISCONNECTED_BACKOFF_SECONDS if state == STATE_DISCONNECTED else FAILED_BACKOFF_SECONDS
    return table[min(max(repeats, 1) - 1, len(table) - 1)]


@dataclass(frozen=True, slots=True)
class _Result:
    outcome: ProbeOutcome
    at_monotonic: float
    at_wall: str
    repeats: int


@dataclass(frozen=True, slots=True)
class DeviceSnapshot:
    """What the API reports under `state["device"]`.

    Every field is present in every state. `available` is true only for
    `available`, and never without `checked_at`, `age_seconds` and `stale`
    beside it, so a cached answer can never be mistaken for a fresh one.
    """

    state: str
    available: bool
    probe_error: str | None
    resolved_label: str | None
    checked_at: str | None
    age_seconds: float | None
    stale: bool
    devices_found: list[dict[str, str]]
    reason: str | None
    probe_seconds: float | None
    last_known_label: str | None
    # True whenever a probe is currently in flight, whatever state is
    # reported alongside it. The state and every other field still describe
    # the last COMPLETED probe -- refreshing never blanks them out to
    # "checking" -- so a phone mid-Rescan keeps showing what it already knew
    # while a fresher answer is on its way.
    refreshing: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "available": self.available,
            "probe_error": self.probe_error,
            "resolved_label": self.resolved_label,
            "checked_at": self.checked_at,
            "age_seconds": self.age_seconds,
            "stale": self.stale,
            "devices_found": [dict(entry) for entry in self.devices_found],
            "reason": self.reason,
            "probe_seconds": self.probe_seconds,
            "last_known_label": self.last_known_label,
            "refreshing": self.refreshing,
        }


class DeviceMonitor:
    """Device state, cached, with at most one probe alive at any moment."""

    def __init__(
        self,
        runner: ProbeRunner,
        *,
        driver: str = "sdrplay",
        device_held: Callable[[], bool] | None = None,
        timeout_seconds: float = DEFAULT_PROBE_TIMEOUT_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        start: bool = True,
    ) -> None:
        self._runner = runner
        self._driver = driver
        self._device_held = device_held or (lambda: False)
        self._timeout_seconds = timeout_seconds
        self._clock = clock

        self._condition = threading.Condition()
        self._result: _Result | None = None
        self._last_label: str | None = None
        self._generation = 0
        self._thread: threading.Thread | None = None
        self._closed = False
        self.probe_count = 0

        if start:
            # The first probe is started here, in the background, so the
            # very first request is answered from memory with "checking"
            # rather than waiting on hardware.
            self.request_refresh(force=True)

    # -- reading -----------------------------------------------------------

    def snapshot(self) -> DeviceSnapshot:
        """The current answer. A memory read: no probe, no wait, no I/O."""
        held = self._device_held()
        with self._condition:
            return self._snapshot_locked(held=held)

    def poll(self) -> DeviceSnapshot:
        """`snapshot()`, plus a background refresh if the answer has aged
        out. The refresh is started, never waited for."""
        self.refresh_if_due()
        return self.snapshot()

    # -- refreshing --------------------------------------------------------

    def refresh_if_due(self) -> bool:
        """Start a probe if the last answer has aged out. Returns whether
        one was started. Never blocks on the probe itself."""
        if self._device_held():
            return False
        with self._condition:
            result = self._result
            if result is not None:
                window = _window(result.outcome.state, result.repeats)
                if window is None or self._clock() - result.at_monotonic < window:
                    return False
            return self._start_locked()

    def request_refresh(self, *, force: bool = False) -> bool:
        """Ask for a probe now. Returns whether one was started.

        `force` is what "Rescan SDR" uses: it ignores the age window. It
        does not, and must not, ignore the device being held by a running
        capture or drive -- probing a device something else already has is
        the exact mistake this whole module exists to avoid. It also does
        not need to know anything about *why* an earlier attempt might
        still be unfinished: whatever the runner itself refuses to do (an
        unreaped child, for `SubprocessProbeRunner`) is decided safely the
        moment `run()` is actually called, on its own terms, and comes back
        as an ordinary `ProbeOutcome` like any other result.
        """
        if not force:
            return self.refresh_if_due()
        if self._device_held():
            return False
        with self._condition:
            return self._start_locked()

    def refresh_declined_reason(self) -> str | None:
        """Why `request_refresh(force=True)` would decline, or None.

        Only reasons this monitor can see for itself: the device is held,
        the monitor is shutting down, or a probe thread is already running.
        Whether the *runner* would refuse to start a real child (an
        unreaped orphan, for `SubprocessProbeRunner`) is not this monitor's
        business to know about or predict -- request_refresh() is free to
        start a thread that calls the runner, and the runner's own answer
        (timed_out / probe_orphaned, same as any other outcome) is what
        actually reports that back, through the ordinary snapshot.
        """
        if self._device_held():
            return "a recording is running and holds the SDR, so it is not being re-checked"
        with self._condition:
            if self._closed:
                return "the field app is shutting down"
            if self._thread is not None and self._thread.is_alive():
                return "an SDR check is already running"
        return None

    def ensure_fresh(self, *, max_age: float, wait: float) -> DeviceSnapshot:
        """A recent answer, waiting no longer than `wait` seconds for one.

        Used by the one caller that is about to open the device for real
        (`FieldService.require_device_ready`, called before a capture or a
        drive is submitted as a job -- never from inside the job itself, so
        this never needs to ask "is the device free" while its own caller is
        the one holding it). It never waits indefinitely: if the probe has
        not reported by the deadline, the caller gets the stale snapshot
        back and can say so.

        Two things make a cached answer unfit to hand back as-is: the
        device being held (nothing here describes it right now -- see
        `_snapshot_locked`'s "busy" branch, and no probe is started either,
        for the same reason `request_refresh` refuses one), and a probe
        already being in flight, whether started by a background refresh or
        a concurrent Rescan. Answering from a cache that is stale relative
        to a check already under way would be answering with the wrong
        number on purpose, so both cases wait for the generation to move
        (or the deadline to pass) rather than taking the fast path.
        """
        if self._device_held():
            with self._condition:
                return self._snapshot_locked(held=True)

        with self._condition:
            result = self._result
            active = self._thread is not None and self._thread.is_alive()
            if (
                not active
                and result is not None
                and self._clock() - result.at_monotonic <= max_age
            ):
                return self._snapshot_locked(held=False)
            generation = self._generation
            self._start_locked()
            deadline = self._clock() + wait
            while self._generation == generation:
                remaining = deadline - self._clock()
                if remaining <= 0:
                    break
                self._condition.wait(remaining)
            return self._snapshot_locked(held=self._device_held())

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        """Stop probing and release the runner. Safe to call twice."""
        with self._condition:
            self._closed = True
            thread = self._thread
            self._condition.notify_all()
        self._runner.close()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=10.0)

    # -- internals ---------------------------------------------------------

    def _start_locked(self) -> bool:
        """Caller holds the condition. At most one probe thread, ever."""
        if self._closed:
            return False
        if self._thread is not None and self._thread.is_alive():
            return False
        thread = threading.Thread(target=self._probe, name="sdr-probe", daemon=True)
        self._thread = thread
        thread.start()
        return True

    def _probe(self) -> None:
        try:
            outcome = self._runner.run(self._driver, timeout_seconds=self._timeout_seconds)
        except Exception as exc:  # noqa: BLE001 -- a broken runner must not wedge the monitor
            # The monitor's contract is that every probe attempt ends in a
            # recorded, waiter-waking result. Without this, a runner bug
            # would let this thread die here: self._result never updates,
            # self._generation never advances, and every reader -- this one
            # included, on the very next poll -- is left on "checking" (or
            # whatever the last answer was) forever, with nothing left to
            # notice or retry.
            outcome = ProbeOutcome(
                state=STATE_FAILED,
                reason=REASON_RUNNER_RAISED,
                detail=f"the SDR probe runner raised {type(exc).__name__}: {exc}",
            )
        now = self._clock()
        wall = datetime.now(UTC).isoformat()
        with self._condition:
            if self._closed:
                return
            previous = self._result
            repeats = (
                previous.repeats + 1
                if previous is not None and previous.outcome.state == outcome.state
                else 1
            )
            self._result = _Result(
                outcome=outcome, at_monotonic=now, at_wall=wall, repeats=repeats
            )
            if outcome.state == STATE_AVAILABLE and outcome.resolved_label:
                self._last_label = outcome.resolved_label
            self.probe_count += 1
            self._generation += 1
            self._condition.notify_all()

    def _snapshot_locked(self, *, held: bool) -> DeviceSnapshot:
        # Caller holds self._condition, so this is a consistent read: the
        # thread cannot go from alive to not (or vice versa) mid-computation.
        refreshing = self._thread is not None and self._thread.is_alive()
        result = self._result
        if result is None:
            if held:
                # Held from before this monitor ever completed a probe --
                # reachable only in a device_held() that is true from
                # construction, since a real FieldService cannot submit a
                # job before its own DeviceMonitor exists. "checking" would
                # be actively misleading here: none is coming while the
                # device stays held.
                return DeviceSnapshot(
                    state=STATE_BUSY,
                    available=False,
                    probe_error=_BUSY_DETAIL,
                    resolved_label=None,
                    checked_at=None,
                    age_seconds=None,
                    stale=True,
                    devices_found=[],
                    reason=None,
                    probe_seconds=None,
                    last_known_label=self._last_label,
                    refreshing=refreshing,
                )
            return DeviceSnapshot(
                state=STATE_CHECKING,
                available=False,
                probe_error=_CHECKING_DETAIL,
                resolved_label=None,
                checked_at=None,
                age_seconds=None,
                stale=False,
                devices_found=[],
                reason=None,
                probe_seconds=None,
                last_known_label=self._last_label,
                refreshing=refreshing,
            )

        age = max(0.0, self._clock() - result.at_monotonic)
        window = _window(result.outcome.state, result.repeats)

        if held:
            # A capture holds the device. Nothing here was measured while it
            # did, so nothing here claims to describe the device now: no
            # label, no device list, and stale by definition. `checked_at`
            # still refers to the last probe that actually ran.
            return DeviceSnapshot(
                state=STATE_BUSY,
                available=False,
                probe_error=_BUSY_DETAIL,
                resolved_label=None,
                checked_at=result.at_wall,
                age_seconds=age,
                stale=True,
                devices_found=[],
                reason=None,
                probe_seconds=result.outcome.duration_seconds,
                last_known_label=self._last_label,
                refreshing=refreshing,
            )

        outcome = result.outcome
        return DeviceSnapshot(
            state=outcome.state,
            available=outcome.state == STATE_AVAILABLE,
            probe_error=None if outcome.state == STATE_AVAILABLE else outcome.detail,
            resolved_label=outcome.resolved_label,
            checked_at=result.at_wall,
            age_seconds=age,
            stale=window is None or age > window,
            devices_found=[dict(entry) for entry in outcome.devices_found],
            reason=outcome.reason,
            probe_seconds=outcome.duration_seconds,
            last_known_label=self._last_label,
            refreshing=refreshing,
        )


__all__ = [
    "AVAILABLE_TTL_SECONDS",
    "DISCONNECTED_BACKOFF_SECONDS",
    "FAILED_BACKOFF_SECONDS",
    "NOT_SUPPORTED_TTL_SECONDS",
    "STATE_BUSY",
    "STATE_CHECKING",
    "DeviceMonitor",
    "DeviceSnapshot",
]
