from __future__ import annotations

from pathlib import Path

import pytest

from dmr_iq_surveyor.survey.profiles import (
    BandProfile,
    ComparisonTolerances,
    ProfileError,
    SiteProfile,
    load_band_profile,
    load_site_profile,
    resolve_band_profile,
    resolve_site_profile,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_loads_shipped_central_800_profile() -> None:
    profile = load_band_profile(REPO_ROOT / "config" / "bands" / "central_800.yaml")
    assert profile.name == "central_800"
    assert profile.start_frequency_hz == 866_000_000.0
    assert profile.stop_frequency_hz == 870_000_000.0
    assert 12500.0 in profile.raster_spacings_hz
    assert 6250.0 in profile.raster_spacings_hz
    settings = profile.detection_settings()
    assert settings.scan_step_hz == 6250.0


def test_loads_shipped_home_example_site_profile() -> None:
    profile = load_site_profile(REPO_ROOT / "config" / "sites" / "home.example.yaml")
    assert profile.site_id == "home"
    assert profile.gain is None
    assert profile.is_gain_comparable is False


def test_resolve_band_profile_by_name_under_repo_config() -> None:
    profile = resolve_band_profile("central_800", base_dir=REPO_ROOT)
    assert profile.name == "central_800"


def test_resolve_site_profile_by_name_under_repo_config() -> None:
    profile = resolve_site_profile("home.example", base_dir=REPO_ROOT)
    assert profile.site_id == "home"


def test_unresolvable_band_profile_raises() -> None:
    with pytest.raises(ProfileError):
        resolve_band_profile("does_not_exist", base_dir=REPO_ROOT)


def test_band_profile_rejects_unknown_top_level_key(tmp_path: Path) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text(
        "name: x\nlabel: x\nstart_frequency_hz: 1\nstop_frequency_hz: 2\nbogus_key: 1\n",
        encoding="utf-8",
    )
    with pytest.raises(ProfileError):
        load_band_profile(path)


def test_band_profile_rejects_unknown_detection_override(tmp_path: Path) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text(
        (
            "name: x\nlabel: x\nstart_frequency_hz: 1\nstop_frequency_hz: 2\n"
            "detection:\n  not_a_real_field: 1\n"
        ),
        encoding="utf-8",
    )
    with pytest.raises(ProfileError):
        load_band_profile(path)


def test_band_profile_validates_stop_greater_than_start() -> None:
    with pytest.raises(ProfileError):
        BandProfile(
            name="x", label="x", start_frequency_hz=2.0, stop_frequency_hz=1.0
        ).validate()


def test_comparison_tolerances_validate_ranges() -> None:
    with pytest.raises(ProfileError):
        ComparisonTolerances(occupancy_delta_pct=150.0).validate()
    with pytest.raises(ProfileError):
        ComparisonTolerances(persistence_delta=2.0).validate()
    ComparisonTolerances().validate()


def test_site_profile_rejects_out_of_range_latitude() -> None:
    with pytest.raises(ProfileError):
        SiteProfile(site_id="x", label="x", latitude=200.0).validate()


def test_site_profile_gain_comparable_requires_both_fields() -> None:
    assert not SiteProfile(site_id="x", label="x", gain_mode="manual").is_gain_comparable
    assert not SiteProfile(site_id="x", label="x", gain=10.0).is_gain_comparable
    assert SiteProfile(site_id="x", label="x", gain_mode="manual", gain=10.0).is_gain_comparable


def test_site_profile_lna_state_defaults_to_none_and_round_trips(tmp_path: Path) -> None:
    """A profile written before this field existed must still load cleanly."""
    profile = load_site_profile(REPO_ROOT / "config" / "sites" / "home.example.yaml")
    assert profile.lna_state is None

    path = tmp_path / "site.yaml"
    path.write_text(
        "site_id: field\nlabel: field\ngain_mode: manual\ngain: 26\nlna_state: 8\n",
        encoding="utf-8",
    )
    loaded = load_site_profile(path)
    assert loaded.gain == 26.0
    assert loaded.lna_state == 8


def test_site_profile_rejects_negative_lna_state() -> None:
    with pytest.raises(ProfileError, match="lna_state"):
        SiteProfile(site_id="x", label="x", lna_state=-1).validate()
