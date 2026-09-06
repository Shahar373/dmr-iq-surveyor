"""`dmr-surveyor web serve`'s gain-default resolution.

This is the seam that broke in the field: the site profile's own `gain`
(the one file the whole field guide tells an operator to edit) was never
read by the web app, so the UI silently showed a hardcoded 40/2 default
instead of a validated, gain-tested value -- exactly the kind of mismatch
the project's own gain-discipline checks exist to catch after the fact,
here caught before a single stop was recorded.
"""

from __future__ import annotations

from dmr_iq_surveyor.cli_web import _resolve_capture_gain
from dmr_iq_surveyor.survey.profiles import SiteProfile


def _profile(**overrides: object) -> SiteProfile:
    base: dict[str, object] = {"site_id": "mobile", "label": "mobile"}
    base.update(overrides)
    return SiteProfile(**base)  # type: ignore[arg-type]


def test_site_profile_gain_seeds_the_default_silently() -> None:
    """The operator already made this choice by editing the file; no warning
    is needed to use it."""
    gain, lna, notices = _resolve_capture_gain(
        None, None, _profile(gain=26.0, lna_state=8)
    )
    assert (gain, lna) == (26.0, 8)
    assert notices == []


def test_an_explicit_cli_flag_overrides_the_site_profile() -> None:
    """An operator who passes --if-gain-reduction explicitly means it,
    even if the site profile also has a value."""
    gain, lna, notices = _resolve_capture_gain(
        30.0, 5, _profile(gain=26.0, lna_state=8)
    )
    assert (gain, lna) == (30.0, 5)
    assert notices == []


def test_neither_source_falls_back_and_says_so() -> None:
    """This is the bug as it actually happened: no CLI flag, and a site
    profile with nothing recorded yet. The old hardcoded 40/2 must still be
    reachable, but silently is not acceptable."""
    gain, lna, notices = _resolve_capture_gain(None, None, _profile())
    assert (gain, lna) == (40.0, 2)
    assert len(notices) == 2
    assert any("gain" in notice and "40" in notice for notice in notices)
    assert any("LNA" in notice and "2" in notice for notice in notices)


def test_a_legacy_profile_with_only_gain_set_is_only_warned_about_lna() -> None:
    """A site profile written before lna_state existed has gain but not
    lna_state -- only the missing half should be reported."""
    gain, lna, notices = _resolve_capture_gain(None, None, _profile(gain=26.0))
    assert (gain, lna) == (26.0, 2)
    assert len(notices) == 1
    assert "LNA" in notices[0]


def test_mixed_cli_and_profile_sources_produce_no_notice() -> None:
    """Every value came from an explicit source (flag or profile); a
    fallback notice would be misleading here."""
    gain, lna, notices = _resolve_capture_gain(30.0, None, _profile(lna_state=8))
    assert (gain, lna) == (30.0, 8)
    assert notices == []
