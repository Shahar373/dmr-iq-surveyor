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
`SoapyIqDevice` (and `probe_soapysdr`) touch the real SoapySDR API.

Gain is expressed the way the SDRplay hardware actually works, via
SoapySDRPlay3's *named* gain elements, not via SoapySDR's generic overall
`setGain(dir, chan, value)` float. Both SDRplay elements are gain
*reductions* (higher = less sensitive) while the generic distributor in
`SoapySDR::Device::setGain` assumes elements are gains, so passing an
overall value both inverts the control and saturates the wrong stage first:
an overall 40 lands on IFGR 59 (maximum IF reduction, worst noise figure)
plus LNA state 1 (RF front end nearly wide open), which is close to the
opposite of a sensible split. Named elements also make a setting exactly
reproducible across sessions and comparable to the SDRplay-native tooling.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Protocol

import numpy as np

# A stalled real device (dropped USB connection, blocked driver) must not
# hang run_capture() forever on repeated empty/timeout reads.
_MAX_CONSECUTIVE_EMPTY_READS = 50

# Per-read timeout. Kept well under a second because a stream that failed to
# activate returns TIMEOUT after sleeping for exactly this long, so a large
# value turns a startup failure into a minutes-long silent wait.
_READ_TIMEOUT_US = 200_000

# SoapySDR error codes (include/SoapySDR/Errors.h). Mirrored here so the
# recoverable-vs-fatal decision is readable without the import in view.
_SOAPY_SDR_TIMEOUT = -1
_SOAPY_SDR_OVERFLOW = -4
_SOAPY_SDR_NOT_SUPPORTED = -5

# SDRplay named gain elements, both gain REDUCTIONS in SDRplay's convention.
GAIN_ELEMENT_IF = "IFGR"  # IF gain reduction in dB, typically 20..59
GAIN_ELEMENT_RF = "RFGR"  # LNA state index, 0..9 on an RSP1B


@dataclass(slots=True)
class DeviceSettings:
    driver: str = "sdrplay"
    sample_rate_hz: float = 2_000_000.0
    center_frequency_hz: float = 868_000_000.0
    if_gain_reduction_db: float | None = None
    lna_state: int | None = None
    agc: bool = False
    antenna: str | None = None
    channel: int = 0
    bandwidth_hz: float | None = None

    def validate(self) -> None:
        if self.sample_rate_hz <= 0:
            raise ValueError("sample_rate_hz must be positive")
        if self.center_frequency_hz <= 0:
            raise ValueError("center_frequency_hz must be positive")
        if self.agc and self.if_gain_reduction_db is not None:
            raise ValueError("if_gain_reduction_db must not be set when agc is enabled")
        if not self.agc and self.if_gain_reduction_db is None:
            raise ValueError("if_gain_reduction_db is required when agc is disabled")
        if self.lna_state is not None and self.lna_state < 0:
            raise ValueError("lna_state must not be negative")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class DeviceProbe:
    available: bool
    requested_driver: str
    resolved_label: str | None
    probe_error: str | None
    devices_found: list[dict[str, str]] = field(default_factory=list)

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
                f"({type(exc).__name__}: {exc}). Run `bash scripts/pi_soapysdr_setup.sh`, "
                "which installs them and links them into this project's virtualenv "
                "(Debian installs them outside it, so a venv cannot see them by default)."
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
                "connected (`lsusb`) and that `SoapySDRUtil --find` lists it. If a "
                "previous capture crashed, the device can stay marked in use until the "
                "API service is restarted: `sudo systemctl restart sdrplay`."
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
    """Real SoapySDR-backed `IqDevice`.

    Not exercised by the unit test suite -- no SDRplay hardware is available
    in the development environment. The behaviours encoded here were derived
    from the SoapySDR and SoapySDRPlay3 sources rather than from a run
    against hardware; `dmr-surveyor survey preflight` exists to confirm the
    device end before a capture depends on it.
    """

    def __init__(self) -> None:
        self._device: Any = None
        self._stream: Any = None
        self._channel: int = 0
        self._empty_read_streak: int = 0
        self._mtu: int = 0
        self._buffer: np.ndarray | None = None
        self.overflow_count: int = 0
        self.applied_settings: dict[str, Any] = {}

    def open(self, settings: DeviceSettings) -> None:
        settings.validate()
        import SoapySDR
        from SoapySDR import SOAPY_SDR_CF32, SOAPY_SDR_RX

        self._channel = settings.channel
        self._device = SoapySDR.Device({"driver": settings.driver})
        try:
            self._configure(settings, SOAPY_SDR_RX)
            self._stream = self._device.setupStream(SOAPY_SDR_RX, SOAPY_SDR_CF32, [self._channel])
            # activateStream returns an error code, it does NOT raise. If
            # sdrplay_api_Init failed (another process holds the device, a
            # USB glitch, a stale API service) the stream stays inactive and
            # every subsequent read merely sleeps out its timeout, which
            # would strand a field operator watching nothing happen.
            code = self._device.activateStream(self._stream)
            if code != 0:
                raise RuntimeError(
                    f"activateStream failed with code {code}: the SDRplay API could not "
                    "initialise the device. Another process may be holding the RSP1B; "
                    "try `sudo systemctl restart sdrplay` and re-run."
                )
            self._mtu = int(self._device.getStreamMTU(self._stream)) or 65_536
            self._buffer = np.empty(self._mtu, dtype=np.complex64)
            self._empty_read_streak = 0
        except Exception:
            # A half-open device must not be left claimed for the next run.
            self.close()
            raise

    def _configure(self, settings: DeviceSettings, rx: int) -> None:
        device = self._device
        channel = self._channel

        device.setSampleRate(rx, channel, settings.sample_rate_hz)
        # setSampleRate does not raise on an unsupported rate -- it logs a
        # warning and leaves the device where it was. Without this read-back
        # the WAV would be labelled with a rate the hardware never used, and
        # every frequency in the resulting survey would be wrong with no
        # indication anywhere.
        actual_rate = float(device.getSampleRate(rx, channel))
        if abs(actual_rate - settings.sample_rate_hz) > 1.0:
            supported = list(device.listSampleRates(rx, channel))
            raise RuntimeError(
                f"device refused sample rate {settings.sample_rate_hz:.0f} S/s and is running "
                f"at {actual_rate:.0f} S/s instead. Supported rates: {supported}"
            )

        device.setFrequency(rx, channel, settings.center_frequency_hz)
        if settings.bandwidth_hz:
            device.setBandwidth(rx, channel, settings.bandwidth_hz)
        if settings.antenna:
            device.setAntenna(rx, channel, settings.antenna)

        # AGC state must be set before the IF gain: SoapySDRPlay3 refuses to
        # apply an IFGR value while AGC is enabled.
        device.setGainMode(rx, channel, settings.agc)

        available = set(device.listGains(rx, channel))
        # The LNA state applies in both modes -- SDRplay's AGC controls the
        # IF reduction only and never touches the LNA.
        if settings.lna_state is not None and GAIN_ELEMENT_RF in available:
            self._set_named_gain(rx, channel, GAIN_ELEMENT_RF, float(settings.lna_state))
        if not settings.agc and settings.if_gain_reduction_db is not None:
            if GAIN_ELEMENT_IF not in available:
                raise RuntimeError(
                    f"device exposes no {GAIN_ELEMENT_IF!r} gain element (has {sorted(available)}); "
                    "manual gain cannot be set predictably on it"
                )
            self._set_named_gain(rx, channel, GAIN_ELEMENT_IF, float(settings.if_gain_reduction_db))

        self.applied_settings = {
            "sample_rate_hz": actual_rate,
            "center_frequency_hz": float(device.getFrequency(rx, channel)),
            "bandwidth_hz": float(device.getBandwidth(rx, channel)),
            "agc": bool(device.getGainMode(rx, channel)),
            "gains": {name: float(device.getGain(rx, channel, name)) for name in sorted(available)},
        }

    def _set_named_gain(self, rx: int, channel: int, name: str, value: float) -> None:
        """Set one named gain element, refusing out-of-range values rather
        than letting the driver clamp them silently."""
        gain_range = self._device.getGainRange(rx, channel, name)
        low, high = float(gain_range.minimum()), float(gain_range.maximum())
        if not low <= value <= high:
            raise ValueError(
                f"{name} value {value:g} is outside this device's supported range "
                f"{low:g}..{high:g}"
            )
        self._device.setGain(rx, channel, name, value)

    def read_stream_chunk(self, max_frames: int) -> np.ndarray:
        assert self._buffer is not None, "read_stream_chunk called before open()"
        frames = min(max_frames, self._mtu)
        status = self._device.readStream(
            self._stream, [self._buffer], frames, timeoutUs=_READ_TIMEOUT_US
        )
        code = status.ret
        if code > 0:
            self._empty_read_streak = 0
            return self._buffer[:code]
        if code == _SOAPY_SDR_OVERFLOW:
            # Expected, not exceptional: the driver's FIFO is only tens of
            # milliseconds deep, so any storage or scheduling stall longer
            # than that overruns it. The driver has already discarded the
            # FIFO; the recording continues with a gap. Counting these is how
            # the operator learns the recording is spliced -- aborting the
            # whole capture over one would throw away the session.
            self.overflow_count += 1
            self._empty_read_streak = 0
            return np.empty(0, dtype=np.complex64)
        if code == _SOAPY_SDR_TIMEOUT:
            self._empty_read_streak += 1
            if self._empty_read_streak > _MAX_CONSECUTIVE_EMPTY_READS:
                raise RuntimeError(
                    f"SoapySDR readStream timed out {self._empty_read_streak} times in a row; "
                    "the device appears to have stalled or been disconnected"
                )
            return np.empty(0, dtype=np.complex64)
        detail = (
            "device removed, or the stream is not active"
            if code == _SOAPY_SDR_NOT_SUPPORTED
            else "unexpected driver error"
        )
        raise RuntimeError(f"SoapySDR readStream failed: ret={code} ({detail})")

    def close(self) -> None:
        try:
            if self._stream is not None and self._device is not None:
                self._device.deactivateStream(self._stream)
                self._device.closeStream(self._stream)
        finally:
            # Release the device explicitly rather than leaving it to the
            # binding's __del__: a traceback or reference cycle holding the
            # last reference would otherwise keep the RSP claimed and make
            # the next capture fail to open it.
            if self._device is not None and hasattr(self._device, "close"):
                self._device.close()
            self._stream = None
            self._device = None
            self._buffer = None


__all__ = [
    "GAIN_ELEMENT_IF",
    "GAIN_ELEMENT_RF",
    "DeviceProbe",
    "DeviceSettings",
    "IqDevice",
    "SoapyIqDevice",
    "probe_soapysdr",
]
