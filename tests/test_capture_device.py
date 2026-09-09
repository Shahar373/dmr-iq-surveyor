from __future__ import annotations

import sys

import pytest

from dmr_iq_surveyor.capture.device import DeviceSettings, device_args_string, probe_soapysdr


def test_probe_soapysdr_reports_unavailable_without_bindings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SoapySDR is not importable, regardless of whether this happens to be
    true of the machine running the suite -- a Raspberry Pi field unit with
    real SoapySDR bindings and an RSP1B attached must see the same "not
    importable" branch of probe_soapysdr() as a laptop with neither. Setting
    a module to None in sys.modules is the standard way to force the next
    `import <name>` to raise ImportError without needing the module to be
    genuinely absent; the probe must report that clearly rather than
    raising, mirroring decode/dsd.py::probe_decoder()."""
    monkeypatch.setitem(sys.modules, "SoapySDR", None)
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


def test_device_args_string_is_the_form_soapysdr_can_actually_parse() -> None:
    """Device() must be given `key=value` text, not a dict.

    Verified against a real RSP1B: `Device({'driver':'sdrplay'})` raised
    "SoapySDR::Device::make() no match" while `Device('driver=sdrplay')`
    opened the device, on the same machine in the same process. The dict
    goes through a SWIG conversion to a C++ Kwargs map that loses the
    contents, so the factory sees no driver key; the string overload is
    parsed by C++ itself.
    """
    assert device_args_string("sdrplay") == "driver=sdrplay"
    assert device_args_string("sdrplay", "240404AF60") == "driver=sdrplay,serial=240404AF60"
    # A blank serial must not produce a trailing empty key.
    assert device_args_string("sdrplay", None) == "driver=sdrplay"
    assert device_args_string("sdrplay", "") == "driver=sdrplay"


def test_device_settings_carries_serial_through_to_the_args_string() -> None:
    settings = DeviceSettings(
        driver="sdrplay",
        if_gain_reduction_db=40.0,
        lna_state=4,
        serial="240404AF60",
    )
    settings.validate()
    assert device_args_string(settings.driver, settings.serial) == (
        "driver=sdrplay,serial=240404AF60"
    )
