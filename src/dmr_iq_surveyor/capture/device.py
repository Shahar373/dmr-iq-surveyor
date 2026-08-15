"""SoapySDR device interaction for live SDRplay capture.

`SoapySDR` (and the SDRplay support that comes from the separately-installed
`SoapySDRPlay3` module) is imported lazily, only inside `probe_soapysdr()`
and `SoapyIqDevice.open()`, never at module import time -- mirroring
`decode/dsd.py`'s `probe_decoder()` pattern for the optional DSD-FME binary.
This keeps the whole test suite importable and passing in environments (like
this one) where SoapySDR is not installed.

`IqDevice` is the small mockable seam between orchestration
(`capture/core.py`) and hardware: `open()` / `read_stream_chunk()` /
`close()`. Tests exercise `run_capture()` against a synthetic stub that
implements this interface and yields deterministic complex64 chunks; only
`SoapyIqDevice` (and `probe_soapysdr`) touch the real SoapySDR API, and
neither is exercised by the unit test suite since no SDRplay hardware is
available here. See the task report for exactly what in this file is
unverified against real hardware.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Protocol

import numpy as np

# A stalled real device (dropped USB connection, blocked driver) must not
# hang run_capture() forever on repeated empty/timeout reads.
_MAX_CONSECUTIVE_EMPTY_READS = 50


@dataclass(slots=True)
class DeviceSettings:
    driver: str = "sdrplay"
    sample_rate_hz: float = 10_000_000.0
    center_frequency_hz: float = 868_000_000.0
    gain_db: float | None = None
    agc: bool = False
    antenna: str | None = None
    channel: int = 0

    def validate(self) -> None:
        if self.sample_rate_hz <= 0:
            raise ValueError("sample_rate_hz must be positive")
        if self.center_frequency_hz <= 0:
            raise ValueError("center_frequency_hz must be positive")
        if self.agc and self.gain_db is not None:
            raise ValueError("gain_db must not be set when agc is enabled")
        if not self.agc and self.gain_db is None:
            raise ValueError("gain_db is required when agc is disabled")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class DeviceProbe:
    available: bool
    requested_driver: str
    resolved_label: str | None
    probe_error: str | None
    devices_found: list[dict[str, str]]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def probe_soapysdr(driver: str = "sdrplay") -> DeviceProbe:
    """Report whether a SoapySDR device matching `driver` is available,
    without raising. Never imports SoapySDR anywhere except inside this
    function, so callers (and the whole test suite) work with SoapySDR
    absent."""
    try:
        import SoapySDR
    except ImportError as exc:
        return DeviceProbe(
            available=False,
            requested_driver=driver,
            resolved_label=None,
            probe_error=(
                "SoapySDR Python bindings are not importable "
                f"({type(exc).__name__}: {exc}). Install python3-soapysdr and the "
                "SoapySDRPlay3 module, then verify with `SoapySDRUtil --find` or "
                '`python3 -c "import SoapySDR; print(SoapySDR.Device.enumerate())"`.'
            ),
            devices_found=[],
        )
    try:
        results = SoapySDR.Device.enumerate({"driver": driver})
        devices = [dict(result) for result in results]
    except Exception as exc:  # noqa: BLE001 -- probing must never crash the CLI
        return DeviceProbe(
            available=False,
            requested_driver=driver,
            resolved_label=None,
            probe_error=f"SoapySDR device enumeration failed: {type(exc).__name__}: {exc}",
            devices_found=[],
        )
    if not devices:
        return DeviceProbe(
            available=False,
            requested_driver=driver,
            resolved_label=None,
            probe_error=(
                f"No SoapySDR device matched driver={driver!r}. Confirm the RSP1B is "
                "connected (`lsusb`), sdrplay_apiService/sdrplay.service is running, "
                "and `SoapySDRUtil --find` lists the device."
            ),
            devices_found=[],
        )
    return DeviceProbe(
        available=True,
        requested_driver=driver,
        resolved_label=devices[0].get("label", driver),
        probe_error=None,
        devices_found=devices,
    )


class IqDevice(Protocol):
    """Mockable interface between capture orchestration and the SDR."""

    def open(self, settings: DeviceSettings) -> None: ...

    def read_stream_chunk(self, max_frames: int) -> np.ndarray: ...

    def close(self) -> None: ...


class SoapyIqDevice:
    """Real SoapySDR-backed `IqDevice`. Not exercised by the unit test
    suite -- no SDRplay hardware is available in this environment. The
    exact SoapySDR Python API calls used here (`Device.enumerate`,
    `setSampleRate`/`setFrequency`/`setGainMode`/`setGain`/`setAntenna`,
    `setupStream`/`activateStream`/`readStream`/`deactivateStream`/
    `closeStream`, the `SOAPY_SDR_RX`/`SOAPY_SDR_CF32` constants) come from
    training-time knowledge of the stable SoapySDR API, not from a run
    against real hardware. Confirm with `SoapySDRUtil --find` first; see the
    task report for the exact confidence caveat.
    """

    def __init__(self) -> None:
        self._device: Any = None
        self._stream: Any = None
        self._channel: int = 0

    def open(self, settings: DeviceSettings) -> None:
        settings.validate()
        import SoapySDR
        from SoapySDR import SOAPY_SDR_CF32, SOAPY_SDR_RX

        self._channel = settings.channel
        self._device = SoapySDR.Device({"driver": settings.driver})
        self._device.setSampleRate(SOAPY_SDR_RX, self._channel, settings.sample_rate_hz)
        self._device.setFrequency(SOAPY_SDR_RX, self._channel, settings.center_frequency_hz)
        if settings.antenna:
            self._device.setAntenna(SOAPY_SDR_RX, self._channel, settings.antenna)

        # AGC off is a hard field-recording requirement (repeatable,
        # comparable gain across sites); setGainMode(False) must run before
        # setGain() so the explicit gain actually takes effect.
        self._device.setGainMode(SOAPY_SDR_RX, self._channel, settings.agc)
        if not settings.agc and settings.gain_db is not None:
            self._device.setGain(SOAPY_SDR_RX, self._channel, settings.gain_db)

        self._stream = self._device.setupStream(SOAPY_SDR_RX, SOAPY_SDR_CF32, [self._channel])
        self._device.activateStream(self._stream)
        self._empty_read_streak = 0

    def read_stream_chunk(self, max_frames: int) -> np.ndarray:
        import SoapySDR

        buffer = np.empty(max_frames, dtype=np.complex64)
        status = self._device.readStream(self._stream, [buffer], max_frames, timeoutUs=1_000_000)
        if status.ret == SoapySDR.SOAPY_SDR_TIMEOUT:
            self._empty_read_streak += 1
            if self._empty_read_streak > _MAX_CONSECUTIVE_EMPTY_READS:
                raise RuntimeError(
                    f"SoapySDR readStream timed out {self._empty_read_streak} times in a row; "
                    "the device appears to have stalled"
                )
            return np.empty(0, dtype=np.complex64)
        if status.ret <= 0:
            raise RuntimeError(f"SoapySDR readStream failed: ret={status.ret} flags={status.flags}")
        self._empty_read_streak = 0
        return buffer[: status.ret]

    def close(self) -> None:
        if self._stream is not None and self._device is not None:
            self._device.deactivateStream(self._stream)
            self._device.closeStream(self._stream)
        self._stream = None
        self._device = None


__all__ = [
    "DeviceProbe",
    "DeviceSettings",
    "IqDevice",
    "SoapyIqDevice",
    "probe_soapysdr",
]
