"""Test doubles for the SDR probe seam.

Every test in this suite that touches device state goes through one of
these. Nothing here starts a process, imports SoapySDR, or depends on what
is plugged into the machine running the suite -- which is the whole point:
the same assertions must hold on a laptop with no SDR and on a Raspberry Pi
field unit with an RSP1A attached.
"""

from __future__ import annotations

import threading
from collections.abc import Callable

from dmr_iq_surveyor.capture._soapy_probe import PROBE_DISCONNECTED
from dmr_iq_surveyor.capture.probe import (
    REASON_TIMED_OUT,
    STATE_AVAILABLE,
    STATE_DISCONNECTED,
    STATE_TIMED_OUT,
    ProbeOutcome,
)

MOCKED_DETAIL = "SoapySDR probing is mocked out for this test suite (driver={driver!r})."


def mocked_absent(driver: str) -> ProbeOutcome:
    """The default: no SDR, and the message says why it is not real."""
    return ProbeOutcome(
        state=STATE_DISCONNECTED,
        reason=PROBE_DISCONNECTED,
        detail=MOCKED_DETAIL.format(driver=driver),
    )


def present(label: str = "SDRplay RSPtest") -> Callable[[str], ProbeOutcome]:
    def _outcome(driver: str) -> ProbeOutcome:
        return ProbeOutcome(
            state=STATE_AVAILABLE,
            resolved_label=label,
            devices_found=[{"driver": driver, "label": label}],
        )

    return _outcome


class StubProbeRunner:
    """A ProbeRunner that answers from a script instead of from hardware.

    `gate` turns it into a probe that hangs: `run()` blocks until the gate
    is set or `timeout_seconds` elapses, and on elapse it returns
    `timed_out` -- the same bounded contract the real runner honours, so a
    test can exercise a stuck SDR without ever having one.
    """

    def __init__(
        self,
        outcome: Callable[[str], ProbeOutcome] | ProbeOutcome | None = None,
        *,
        gate: threading.Event | None = None,
    ) -> None:
        if outcome is None:
            self._outcome: Callable[[str], ProbeOutcome] = mocked_absent
        elif isinstance(outcome, ProbeOutcome):
            self._outcome = lambda _driver, fixed=outcome: fixed
        else:
            self._outcome = outcome
        self.gate = gate
        self.calls = 0
        self.drivers: list[str] = []
        self.timeouts: list[float] = []
        self.closed = False
        self.entered = threading.Event()
        self._lock = threading.Lock()

    def set_outcome(self, outcome: Callable[[str], ProbeOutcome] | ProbeOutcome) -> None:
        if isinstance(outcome, ProbeOutcome):
            self._outcome = lambda _driver, fixed=outcome: fixed
        else:
            self._outcome = outcome

    def run(self, driver: str, *, timeout_seconds: float) -> ProbeOutcome:
        with self._lock:
            self.calls += 1
            self.drivers.append(driver)
            self.timeouts.append(timeout_seconds)
        self.entered.set()
        if self.gate is not None and not self.gate.wait(timeout_seconds):
            return ProbeOutcome(
                state=STATE_TIMED_OUT,
                reason=REASON_TIMED_OUT,
                detail=f"the SDR check did not finish within {timeout_seconds:g} s (stub).",
                duration_seconds=timeout_seconds,
            )
        return self._outcome(driver)

    def close(self) -> None:
        self.closed = True
        if self.gate is not None:
            self.gate.set()


__all__ = ["MOCKED_DETAIL", "StubProbeRunner", "mocked_absent", "present"]
