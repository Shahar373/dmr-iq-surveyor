from __future__ import annotations

import pytest

from dmr_iq_surveyor.capture.device import DeviceSettings, probe_soapysdr


def test_probe_soapysdr_reports_unavailable_without_bindings() -> None:
    """SoapySDR is not installed in this environment (by design -- see
    capture/device.py's docstring); the probe must report that clearly
    rather than raising, mirroring decode/dsd.py::probe_decoder()."""
    probe = probe_soapysdr("sdrplay")
    assert probe.available is False
    assert probe.requested_driver == "sdrplay"
    assert probe.resolved_label is None
    assert probe.probe_error is not None
    assert "SoapySDR" in probe.probe_error
    assert probe.devices_found == []
    # to_dict() must not raise and must round-trip the same fields.
    payload = probe.to_dict()
    assert payload["available"] is False
    assert payload["requested_driver"] == "sdrplay"


def test_device_settings_accepts_manual_gain_with_agc_off() -> None:
    settings = DeviceSettings(
        sample_rate_hz=10_000_000.0,
        center_frequency_hz=868_000_000.0,
        agc=False,
        if_gain_reduction_db=30.0,
    )
    settings.validate()  # must not raise


def test_device_settings_accepts_agc_with_no_gain() -> None:
    settings = DeviceSettings(
        sample_rate_hz=10_000_000.0,
        center_frequency_hz=868_000_000.0,
        agc=True,
        if_gain_reduction_db=None,
    )
    settings.validate()  # must not raise


def test_device_settings_rejects_agc_and_gain_together() -> None:
    settings = DeviceSettings(
        sample_rate_hz=10_000_000.0,
        center_frequency_hz=868_000_000.0,
        agc=True,
        if_gain_reduction_db=30.0,
    )
    with pytest.raises(ValueError):
        settings.validate()


def test_device_settings_requires_gain_when_agc_off() -> None:
    settings = DeviceSettings(
        sample_rate_hz=10_000_000.0,
        center_frequency_hz=868_000_000.0,
        agc=False,
        if_gain_reduction_db=None,
    )
    with pytest.raises(ValueError):
        settings.validate()


def test_device_settings_rejects_non_positive_sample_rate() -> None:
    settings = DeviceSettings(sample_rate_hz=0.0, center_frequency_hz=868_000_000.0, agc=True)
    with pytest.raises(ValueError):
        settings.validate()
