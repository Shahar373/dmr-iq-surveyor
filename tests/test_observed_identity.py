"""What the radio said it is, kept apart from what anyone declared it to be.

A capture on the Pi stored `hardware_json.identity` as `{"driver":
"sdrplay"}` while the device in the field was reporting both a serial and a
label. The driver name was the *requested* one -- the default, in fact -- so
the one bucket meant to hold observations held nothing observed at all, and a
campaign could not say afterwards which of two RSPs recorded which stop.

The rule these pin is the one the other three buckets already follow:
`identity` holds what the device answered. A serial typed on the command line
selects a radio and is recorded as such; a serial or receiver name in a
hardware profile is a declaration and stays in `declared`. Neither may appear
as an observation, however precise it is.
"""

from __future__ import annotations

from typing import Any

import pytest

from dmr_iq_surveyor.capture.device import device_identity
from dmr_iq_surveyor.survey.profiles import HardwareProfile, SiteProfile
from dmr_iq_surveyor.survey.provenance import (
    declared_bucket,
    hardware_from_capture_manifest,
    hardware_provenance,
    identity_value,
    normalise_hardware,
    with_declared,
)


class FakeDevice:
    """A SoapySDR device as far as identity is concerned.

    Counts the questions, because asking twice would mean a second open or a
    second probe -- and the SDRplay API hands the radio to one client at a
    time.
    """

    def __init__(self, info: Any = None, key: Any = None, *, raises: bool = False) -> None:
        self._info = {} if info is None else info
        self._key = key
        self._raises = raises
        self.info_calls = 0
        self.key_calls = 0

    def getHardwareInfo(self) -> Any:  # noqa: N802 -- SoapySDR's own spelling
        self.info_calls += 1
        if self._raises:
            raise RuntimeError("the driver does not implement this")
        return self._info

    def getHardwareKey(self) -> Any:  # noqa: N802 -- SoapySDR's own spelling
        self.key_calls += 1
        if self._raises:
            raise RuntimeError("the driver does not implement this")
        return self._key


# -- what the device says -------------------------------------------------


def test_a_device_that_answers_is_recorded_as_it_answered() -> None:
    device = FakeDevice({"serial": "230405A498", "label": "SDRplay Dev0 RSP1A 230405A498"})

    assert device_identity(device) == {
        "serial": "230405A498",
        "label": "SDRplay Dev0 RSP1A 230405A498",
    }


def test_the_hardware_key_answers_for_the_label_when_the_info_has_none() -> None:
    device = FakeDevice({"serial": "230405A498"}, "RSP1A")

    assert device_identity(device) == {"serial": "230405A498", "label": "RSP1A"}


def test_a_label_in_the_info_is_not_replaced_by_the_coarser_hardware_key() -> None:
    """The info mapping names this unit; the hardware key names the model."""
    device = FakeDevice({"label": "SDRplay Dev0 RSP1A 230405A498"}, "RSP1A")

    assert device_identity(device)["label"] == "SDRplay Dev0 RSP1A 230405A498"
    assert device.key_calls == 0


@pytest.mark.parametrize("answer", ["", "   ", None, 3, ["RSP1A"], {"serial": "x"}])
def test_an_answer_that_is_not_a_name_is_left_out_rather_than_stored(answer: Any) -> None:
    """Absent says "not observed". An empty string in this bucket would read
    as a serial the radio reported and nobody can look up."""
    device = FakeDevice({"serial": answer, "label": answer})

    assert device_identity(device) == {}


def test_a_driver_that_refuses_every_question_costs_nothing() -> None:
    """A capture is never lost to a question about a name."""
    device = FakeDevice(raises=True)

    assert device_identity(device) == {}


def test_a_device_object_that_has_no_identity_methods_at_all_is_fine() -> None:
    class Minimal:
        pass

    assert device_identity(Minimal()) == {}


def test_the_device_is_asked_once_per_answer() -> None:
    device = FakeDevice({"serial": "230405A498", "label": "RSP1A"})

    device_identity(device)

    assert device.info_calls == 1
    assert device.key_calls == 0


# -- what reaches the database --------------------------------------------


def _manifest(**extra: Any) -> dict[str, Any]:
    manifest = {
        "settings": {
            "driver": "sdrplay",
            "serial": None,
            "center_frequency_hz": 868_200_000.0,
            "sample_rate_hz": 768_000.0,
        },
        "device_settings_applied": {"center_frequency_hz": 868_200_000.0},
    }
    manifest.update(extra)
    return manifest


def test_the_capture_report_carries_the_identity_into_the_stored_blob() -> None:
    blob = hardware_from_capture_manifest(
        _manifest(device_identity={"serial": "230405A498", "label": "RSP1A"})
    )

    assert identity_value(blob, "serial") == "230405A498"
    assert identity_value(blob, "label") == "RSP1A"
    assert identity_value(blob, "driver") == "sdrplay"


def test_a_report_from_before_this_existed_reads_exactly_as_it_did() -> None:
    """No backfill, and no change to what an older capture report means."""
    blob = hardware_from_capture_manifest(_manifest())

    assert blob["identity"] == {"driver": "sdrplay"}


def test_what_the_device_said_outranks_what_it_was_asked_for() -> None:
    """`--serial` pins which radio to open. When the radio answers, what it
    says it is wins -- the observation, not the request."""
    manifest = _manifest(device_identity={"serial": "230405A498"})
    manifest["settings"]["serial"] = "1234567890"

    blob = hardware_from_capture_manifest(manifest)

    assert identity_value(blob, "serial") == "230405A498"


@pytest.mark.parametrize(
    "offered",
    [
        {"serial": ["230405A498"]},
        {"serial": {"value": "230405A498"}},
        "230405A498",
        None,
        42,
    ],
)
def test_a_malformed_identity_is_ignored_rather_than_stored_or_raised(offered: Any) -> None:
    """A report nobody can parse is not evidence about a radio, and a blob
    that raised here would take the whole survey with it."""
    blob = hardware_from_capture_manifest(_manifest(device_identity=offered))

    assert identity_value(blob, "serial") is None
    assert normalise_hardware(blob) == blob


def test_the_identity_does_not_change_which_evidence_the_blob_claims() -> None:
    """`source` names the strongest bucket that carries a *setting*. A serial
    is not a reading of one, so recording it must not promote a run."""
    without = hardware_from_capture_manifest(_manifest())
    with_identity = hardware_from_capture_manifest(
        _manifest(device_identity={"serial": "230405A498"})
    )

    assert with_identity["source"] == without["source"]


# -- observed is not declared ---------------------------------------------


PROFILE = HardwareProfile(
    hardware_id="rsp1a_field",
    label="Field RSP1A",
    receiver="SDRplay RSP1A (serial 999999ZZZZ)",
    antenna="whip",
    gain_mode="manual",
    if_gain_reduction_db=25.0,
    lna_state=2,
)

SITE = SiteProfile(
    site_id="g4",
    label="G4",
    latitude=32.05,
    longitude=34.79,
    antenna="whip",
    receiver="SDRplay RSP1A",
)


def test_a_declared_receiver_never_appears_as_an_observation() -> None:
    """The profile is the most precise thing many runs have, and it is still
    a declaration: it says what the operator believes is plugged in."""
    blob = with_declared(
        hardware_from_capture_manifest(_manifest()), declared_bucket(SITE, PROFILE)
    )

    assert blob["declared"]["receiver"] == "SDRplay RSP1A (serial 999999ZZZZ)"
    assert blob["declared"]["hardware_id"] == "rsp1a_field"
    assert identity_value(blob, "serial") is None
    assert identity_value(blob, "label") is None
    assert "999999ZZZZ" not in str(blob["identity"])


def test_observed_and_declared_sit_side_by_side_without_merging() -> None:
    """The case the campaign actually cares about: the profile says which
    radio was meant to be used and the radio says which one was."""
    blob = with_declared(
        hardware_from_capture_manifest(
            _manifest(device_identity={"serial": "230405A498", "label": "RSP1A"})
        ),
        declared_bucket(SITE, PROFILE),
    )

    assert identity_value(blob, "serial") == "230405A498"
    assert blob["declared"]["receiver"] == "SDRplay RSP1A (serial 999999ZZZZ)"
    assert "serial" not in blob["declared"]


# -- the whole way through a capture --------------------------------------


class _TalkativeDevice:
    """An `IqDevice` that also reports what it is, as `SoapyIqDevice` does."""

    def __init__(self, identity: dict[str, str]) -> None:
        self.observed_identity = dict(identity)
        self.applied_settings: dict[str, Any] = {}
        self.opened_with: Any = None

    def open(self, settings: Any) -> None:
        settings.validate()
        self.opened_with = settings
        self.applied_settings = {
            "sample_rate_hz": settings.sample_rate_hz,
            "center_frequency_hz": settings.center_frequency_hz,
        }

    def read_stream_chunk(self, max_frames: int) -> Any:
        import numpy as np

        return np.zeros(max_frames, dtype=np.complex64)

    def close(self) -> None:
        return None


class _SilentDevice(_TalkativeDevice):
    """A device exposing no identity attribute at all -- an older stub, or a
    driver that answered nothing."""

    def __init__(self) -> None:
        super().__init__({})
        del self.observed_identity


def _capture_settings() -> Any:
    from dmr_iq_surveyor.capture.core import CaptureSettings

    return CaptureSettings(
        center_frequency_hz=868_000_000.0,
        sample_rate_hz=200_000.0,
        duration_seconds=0.2,
        if_gain_reduction_db=25.0,
    )


def test_a_capture_records_what_the_radio_said_it_was(tmp_path: Any) -> None:
    """End to end: the device answers, the capture report carries it, and
    the blob the database stores says it was observed."""
    from dmr_iq_surveyor.capture.core import run_capture

    device = _TalkativeDevice({"serial": "230405A498", "label": "SDRplay Dev0 RSP1A"})

    manifest = run_capture(tmp_path, settings=_capture_settings(), device=device)

    assert manifest["device_identity"] == {
        "serial": "230405A498",
        "label": "SDRplay Dev0 RSP1A",
    }
    blob = hardware_from_capture_manifest(manifest)
    assert identity_value(blob, "serial") == "230405A498"
    assert identity_value(blob, "label") == "SDRplay Dev0 RSP1A"


def test_a_capture_from_a_device_that_says_nothing_still_completes(
    tmp_path: Any,
) -> None:
    from dmr_iq_surveyor.capture.core import run_capture

    manifest = run_capture(tmp_path, settings=_capture_settings(), device=_SilentDevice())

    assert manifest["device_identity"] == {}
    assert manifest["frame_count"] > 0
    assert identity_value(hardware_from_capture_manifest(manifest), "serial") is None


def test_attaching_a_declaration_later_leaves_the_identity_alone() -> None:
    before = hardware_provenance(identity={"driver": "sdrplay", "serial": "230405A498"})

    after = with_declared(before, declared_bucket(SITE, PROFILE))

    assert after["identity"] == before["identity"]
