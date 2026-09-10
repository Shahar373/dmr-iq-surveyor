"""The hardware profile, and the one resolver every gain reading goes through.

Two things are under test here and they pull in opposite directions. A
hardware profile has to be *authoritative* -- it is the file that says what
the receiver is set to across a whole round -- and it has to be *only a
declaration*, because nothing but the radio's own read-back may claim to be
what the receiver was actually set to. Every test below pins one or the other.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dmr_iq_surveyor.cli_web import (
    ORIGIN_FALLBACK,
    ORIGIN_FLAG,
    ORIGIN_HARDWARE,
    ORIGIN_SITE,
    resolve_capture_gain,
)
from dmr_iq_surveyor.survey.profiles import (
    HardwareProfile,
    ProfileError,
    SiteProfile,
    load_hardware_profile,
    resolve_hardware_profile,
)
from dmr_iq_surveyor.survey.provenance import (
    SOURCE_APPLIED,
    SOURCE_DECLARED,
    SOURCE_DECLARED_SITE_ROW,
    declared_bucket,
    hardware_provenance,
    if_gain_reading,
    lna_state_reading,
    receiver_settings,
    requested_bucket,
)

REPO_ROOT = Path(__file__).resolve().parents[1]

HARDWARE_YAML = """
hardware_id: field_rsp1a
label: "Field RSP1A"
receiver: "SDRplay RSP1A"
antenna: "Discone"
gain_mode: manual
if_gain_reduction_db: 33.0
lna_state: 4
"""


def _write(tmp_path: Path, body: str = HARDWARE_YAML, name: str = "field_rsp1a") -> Path:
    directory = tmp_path / "config" / "hardware"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.yaml"
    path.write_text(body, encoding="utf-8")
    return path


def _site(**overrides: object) -> SiteProfile:
    base: dict[str, object] = {"site_id": "mobile", "label": "mobile"}
    base.update(overrides)
    return SiteProfile(**base)  # type: ignore[arg-type]


# -- the file ---------------------------------------------------------------


def test_a_valid_hardware_profile_parses(tmp_path: Path) -> None:
    profile = load_hardware_profile(_write(tmp_path))

    assert profile.hardware_id == "field_rsp1a"
    assert profile.if_gain_reduction_db == 33.0
    assert profile.lna_state == 4
    assert profile.declares_gain


def test_lna_state_alone_does_not_declare_a_gain() -> None:
    """`declares_gain` gates `run_survey`'s "not gain-comparable" warning
    (see `survey/pipeline.py`). `lna_state` sets the noise figure ahead of
    the IF stage -- it answers a different question from `gain` -- so a
    profile that names only `lna_state` has not declared the value every
    gain reader (`if_gain_reading`) actually compares across runs, and must
    not silently suppress that warning."""
    lna_only = HardwareProfile(hardware_id="field", label="Field", lna_state=4)
    assert not lna_only.declares_gain

    gain_only = HardwareProfile(hardware_id="field", label="Field", if_gain_reduction_db=33.0)
    assert gain_only.declares_gain

    both = HardwareProfile(
        hardware_id="field", label="Field", if_gain_reduction_db=33.0, lna_state=4
    )
    assert both.declares_gain


def test_an_unknown_key_is_an_error_not_a_silent_no_op(tmp_path: Path) -> None:
    """A misspelled setting is one an operator believes is in force when it
    is not -- the same rule band and site profiles have always had."""
    with pytest.raises(ProfileError, match="Unknown keys"):
        load_hardware_profile(_write(tmp_path, HARDWARE_YAML + "gain: 40\n"))


@pytest.mark.parametrize(
    ("line", "match"),
    [
        ("if_gain_reduction_db: -3\n", "negative"),
        ("if_gain_reduction_db: .nan\n", "finite"),
        ("lna_state: -1\n", "lna_state"),
        ("gain_mode: sometimes\n", "gain_mode"),
    ],
)
def test_a_value_that_is_not_a_receiver_setting_is_refused(
    tmp_path: Path, line: str, match: str
) -> None:
    body = "hardware_id: x\nlabel: x\n" + line
    with pytest.raises(ProfileError, match=match):
        load_hardware_profile(_write(tmp_path, body))


def test_a_profile_without_an_id_or_label_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ProfileError, match="requires"):
        load_hardware_profile(_write(tmp_path, "receiver: something\n"))


def test_a_profile_resolves_by_name_and_by_path(tmp_path: Path) -> None:
    """The same two-step shape as `--band` and `--site`, so an operator who
    knows one knows all three."""
    path = _write(tmp_path)

    assert load_hardware_profile(path).hardware_id == "field_rsp1a"
    assert resolve_hardware_profile("field_rsp1a", base_dir=tmp_path).hardware_id == (
        "field_rsp1a"
    )
    assert resolve_hardware_profile(str(path)).hardware_id == "field_rsp1a"


def test_an_unresolvable_profile_says_where_it_looked(tmp_path: Path) -> None:
    with pytest.raises(ProfileError, match="config/hardware"):
        resolve_hardware_profile("nope", base_dir=tmp_path)


def test_the_shipped_example_is_valid() -> None:
    """It is documentation an operator is told to copy; a stale example is a
    worse starting point than none."""
    profile = load_hardware_profile(REPO_ROOT / "config" / "hardware" / "example.yaml")

    assert profile.hardware_id == "example"
    assert profile.gain_mode == "manual"
    assert profile.declares_gain


# -- it is a declaration, never a reading -----------------------------------


def test_a_hardware_profile_declares_and_never_claims_to_be_applied() -> None:
    """The line that must not move. However precise the file is, it says what
    somebody intended; only the radio's read-back is what the radio did."""
    profile = HardwareProfile(
        hardware_id="field", label="Field", if_gain_reduction_db=33.0, lna_state=4
    )

    declared = declared_bucket(_site(), profile)

    assert declared["gain"] == 33.0
    assert declared["lna_state"] == 4
    assert if_gain_reading(hardware_provenance(declared=declared)).source == SOURCE_DECLARED
    assert not if_gain_reading(hardware_provenance(declared=declared)).measured


def test_a_read_back_still_outranks_the_hardware_profile() -> None:
    """The precedence in one assertion: applied beats declared, whatever the
    declaration is written in."""
    profile = HardwareProfile(
        hardware_id="field", label="Field", if_gain_reduction_db=33.0, lna_state=4
    )
    blob = hardware_provenance(
        applied={"gains": {"IFGR": 25.0, "RFGR": 2.0}},
        requested=requested_bucket(if_gain_reduction_db=30.0, lna_state=3),
        declared=declared_bucket(_site(), profile),
    )

    assert if_gain_reading(blob).value == 25.0
    assert if_gain_reading(blob).source == SOURCE_APPLIED
    assert lna_state_reading(blob).value == 2


def test_the_hardware_profile_outranks_the_site_profile_field_by_field() -> None:
    """Gain belongs to the radio, not to the place. What the hardware profile
    leaves unset the site profile still answers, so naming one never takes
    information away from a run."""
    site = _site(receiver="Old radio", antenna="Whip", gain=40.0, lna_state=2)
    profile = HardwareProfile(
        hardware_id="field", label="Field", receiver="RSP1A", if_gain_reduction_db=33.0
    )

    declared = declared_bucket(site, profile)

    assert declared["gain"] == 33.0  # the hardware profile's
    assert declared["receiver"] == "RSP1A"  # the hardware profile's
    assert declared["lna_state"] == 2  # unset in hardware; the site's survives
    assert declared["antenna"] == "Whip"  # likewise
    assert declared["hardware_id"] == "field"
    assert declared["site_id"] == "mobile"


def test_without_a_hardware_profile_the_declaration_is_exactly_what_it_was() -> None:
    """Backward compatibility for every deployment that has no such file."""
    site = _site(receiver="Old radio", antenna="Whip", gain=40.0, lna_state=2)

    assert declared_bucket(site) == declared_bucket(site, None)
    assert "hardware_id" not in declared_bucket(site)


# -- the one reading resolver ------------------------------------------------


def test_a_run_with_provenance_never_reads_the_mutable_site_row() -> None:
    """`sites` is current state -- `upsert_site` rewrites it on every run --
    so a run that carries its own declaration must answer from that."""
    blob = hardware_provenance(
        declared=declared_bucket(_site(gain=33.0, lna_state=4))
    )
    row = {
        "survey_run_id": "r",
        "hardware_json": json.dumps(blob),
        "gain": 40.0,
        "lna_state": 2,
    }

    resolved = receiver_settings(row)

    assert resolved.if_gain_reduction.value == 33.0
    assert resolved.if_gain_reduction.source == SOURCE_DECLARED
    assert not resolved.from_legacy_site_row


def test_a_run_without_provenance_falls_back_and_is_labelled_apart() -> None:
    row = {
        "survey_run_id": "r",
        "hardware_json": "{}",
        "gain": 40.0,
        "lna_state": 2,
    }

    resolved = receiver_settings(row)

    assert resolved.if_gain_reduction.value == 40.0
    assert resolved.if_gain_reduction.source == SOURCE_DECLARED_SITE_ROW
    assert resolved.from_legacy_site_row
    assert "site row" in resolved.if_gain_reduction.render(" dB")


def test_a_row_that_never_joined_the_site_table_simply_has_no_legacy_tier() -> None:
    """A caller that did not select `sites.gain` loses that tier rather than
    crashing on the missing column."""
    resolved = receiver_settings({"survey_run_id": "r", "hardware_json": "{}"})

    assert not resolved.if_gain_reduction.known
    assert resolved.if_gain_reduction.render() == "not recorded"


# -- the one capture-time resolver -------------------------------------------


def test_the_hardware_profile_seeds_the_capture_gain() -> None:
    profile = HardwareProfile(
        hardware_id="field", label="Field", if_gain_reduction_db=33.0, lna_state=4
    )

    gain = resolve_capture_gain(None, None, _site(gain=40.0, lna_state=2), profile)

    assert (gain.if_gain_reduction_db, gain.lna_state) == (33.0, 4)
    assert gain.if_gain_origin == ORIGIN_HARDWARE
    assert gain.lna_origin == ORIGIN_HARDWARE
    assert gain.notices == []
    assert not gain.unconfirmed


def test_a_flag_still_wins_and_says_it_broke_the_round() -> None:
    """An operator at the radio means it. But a round whose stops were not all
    taken at one gain is exactly what the drift check hunts for afterwards, so
    it is said out loud now rather than discovered later."""
    profile = HardwareProfile(
        hardware_id="field", label="Field", if_gain_reduction_db=33.0, lna_state=4
    )

    gain = resolve_capture_gain(20.0, None, _site(), profile)

    assert gain.if_gain_reduction_db == 20.0
    assert gain.if_gain_origin == ORIGIN_FLAG
    assert gain.lna_origin == ORIGIN_HARDWARE
    assert any("overrides" in notice and "field" in notice for notice in gain.notices)


def test_a_flag_that_agrees_with_the_profile_is_not_a_conflict() -> None:
    profile = HardwareProfile(hardware_id="field", label="Field", if_gain_reduction_db=33.0)

    gain = resolve_capture_gain(33.0, None, _site(lna_state=2), profile)

    assert gain.notices == []
    assert gain.if_gain_origin == ORIGIN_FLAG


def test_the_site_profile_answers_what_the_hardware_profile_leaves_unset() -> None:
    profile = HardwareProfile(hardware_id="field", label="Field", if_gain_reduction_db=33.0)

    gain = resolve_capture_gain(None, None, _site(gain=40.0, lna_state=7), profile)

    assert (gain.if_gain_reduction_db, gain.lna_state) == (33.0, 7)
    assert gain.if_gain_origin == ORIGIN_HARDWARE
    assert gain.lna_origin == ORIGIN_SITE
    assert gain.notices == []


def test_nothing_configured_anywhere_still_falls_back_and_reports_it() -> None:
    gain = resolve_capture_gain(None, None, _site(), None)

    assert (gain.if_gain_reduction_db, gain.lna_state) == (40.0, 2)
    assert gain.if_gain_origin == ORIGIN_FALLBACK
    assert gain.unconfirmed
    assert len(gain.notices) == 2


def test_every_resolution_names_the_origin_of_both_halves() -> None:
    """A gain whose source is invisible is one nobody checks."""
    profile = HardwareProfile(hardware_id="field", label="Field", if_gain_reduction_db=33.0)

    described = resolve_capture_gain(None, 6, _site(), profile).describe()

    assert "hardware profile" in described
    assert "command line" in described
