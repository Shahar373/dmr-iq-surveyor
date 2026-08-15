"""Live SDRplay IQ capture via SoapySDR, streamed straight to a WAV file.

This package exists because of one explicit, one-off exception to this
project's documented "no premature live acquisition" principle
(see `CLAUDE.md`), authorized for a single field-recording request: capture
and `survey run` in one command. It does not change the general rule for
future work.
"""

from dmr_iq_surveyor.capture.core import CaptureSettings, run_capture, run_capture_and_survey
from dmr_iq_surveyor.capture.device import DeviceProbe, DeviceSettings, probe_soapysdr

__all__ = [
    "CaptureSettings",
    "DeviceProbe",
    "DeviceSettings",
    "probe_soapysdr",
    "run_capture",
    "run_capture_and_survey",
]
