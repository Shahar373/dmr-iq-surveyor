"""The SoapySDR device probe, as a leaf module with no heavy imports.

This is deliberately the only part of the capture layer that imports
nothing -- not numpy, not the rest of this package, not even
`dmr_iq_surveyor` itself. Two callers depend on that:

* `capture/device.py`, which wraps `probe_payload()` in a `DeviceProbe` and
  exposes it as `probe_soapysdr()`, unchanged for the CLI and preflight;
* the field app, which runs this file **as a script, by path**, in a child
  process it can kill (`capture/probe.py`). Running it by path rather than
  as `python -m dmr_iq_surveyor.capture...` skips
  `dmr_iq_surveyor/capture/__init__.py` and the scipy/matplotlib chain it
  pulls in through `survey.pipeline` -- measured at 1.2 s on a development
  machine and more on a Raspberry Pi, which would eat most of the probe's
  timeout budget before SoapySDR was even reached.

Keeping the messages here, rather than duplicating them in the child, is
the point: a single wording for "the bindings are missing", "no device
matched" and "enumeration raised", whoever asks.
"""

from __future__ import annotations

from typing import Any

# Why a probe found no usable device. Carried through to the caller so it can
# tell "the bindings are not installed" from "the device is unplugged" from
# "enumeration raised" without matching on the English of `probe_error` --
# which matters most across the process boundary, where JSON is all there is.
# Absent (None) on a successful probe.
PROBE_NOT_SUPPORTED = "not_supported"
PROBE_DISCONNECTED = "disconnected"
PROBE_FAILED = "failed"


def probe_payload(driver: str = "sdrplay") -> dict[str, Any]:
    """Probe for a SoapySDR device matching `driver`, as plain data.

    Never raises, and never imports SoapySDR anywhere except inside this
    function, so callers (and the whole test suite) work with SoapySDR
    absent. The returned mapping is exactly the field set of
    `capture.device.DeviceProbe`.
    """
    try:
        import SoapySDR
    except ImportError as exc:
        return {
            "available": False,
            "requested_driver": driver,
            "resolved_label": None,
            "probe_error": (
                "SoapySDR Python bindings are not importable "
                f"({type(exc).__name__}: {exc}). Run `bash scripts/pi_soapysdr_setup.sh`, "
                "which installs them and links them into this project's virtualenv "
                "(Debian installs them outside it, so a venv cannot see them by default)."
            ),
            "devices_found": [],
            "reason": PROBE_NOT_SUPPORTED,
        }
    try:
        results = SoapySDR.Device.enumerate({"driver": driver})
        devices = [dict(result) for result in results]
    except Exception as exc:  # noqa: BLE001 -- probing must never crash the CLI
        return {
            "available": False,
            "requested_driver": driver,
            "resolved_label": None,
            "probe_error": f"SoapySDR device enumeration failed: {type(exc).__name__}: {exc}",
            "devices_found": [],
            "reason": PROBE_FAILED,
        }
    if not devices:
        return {
            "available": False,
            "requested_driver": driver,
            "resolved_label": None,
            "probe_error": (
                f"No SoapySDR device matched driver={driver!r}. Confirm the SDRplay "
                "device (RSP1A/RSP1B) is connected (`lsusb`) and that `SoapySDRUtil "
                "--find` lists it. If a previous capture crashed, the device can stay "
                "marked in use until the API service is restarted: "
                "`sudo systemctl restart sdrplay`."
            ),
            "devices_found": [],
            "reason": PROBE_DISCONNECTED,
        }
    return {
        "available": True,
        "requested_driver": driver,
        "resolved_label": devices[0].get("label", driver),
        "probe_error": None,
        "devices_found": devices,
        "reason": None,
    }


def _main() -> int:
    """Child-process entry point: one JSON object on stdout, nothing else.

    Invoked as `python <this file> <driver>`. Anything SoapySDR itself
    prints goes to stderr or is tolerated by the parent's parser, which
    reads the last JSON object on stdout.
    """
    import json
    import sys

    driver = sys.argv[1] if len(sys.argv) > 1 else "sdrplay"
    # Newline-delimited on both sides: libSoapySDR and the SDRplay module
    # write to the same stdout from C++, with their own buffering, and an
    # unterminated line of theirs would otherwise be glued to this JSON.
    sys.stdout.write("\n" + json.dumps(probe_payload(driver)) + "\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
