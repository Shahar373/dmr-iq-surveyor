"""`web serve`'s shared-token resolution, and what may be printed of it.

The token is a bearer credential that the phone carries in a bookmarked URL,
so it is only as private as the places it gets written down. Running the app
as a systemd service adds two such places that did not exist when it was
started by hand: the process command line, which `systemctl status` prints
back in the cgroup tree, and the journal, which captures the startup banner.
`--token-file` exists to keep the value out of both, and these tests pin the
parts of that which are easy to regress -- especially the refusals, since the
dangerous failure is not a crash but an app that quietly serves without
authentication.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from dmr_iq_surveyor.cli_web import (
    ResolvedToken,
    TokenError,
    resolve_token,
    token_query_suffix,
    web_serve,
)


def _token_file(tmp_path: Path, contents: str, mode: int = 0o600) -> Path:
    path = tmp_path / "token"
    path.write_text(contents, encoding="utf-8")
    path.chmod(mode)
    return path


def test_a_token_file_supplies_the_token_so_it_never_reaches_the_command_line(
    tmp_path: Path,
) -> None:
    """The whole point of the flag: the value comes from the filesystem, so
    the service unit names a path and nothing else."""
    resolved = resolve_token(None, _token_file(tmp_path, "s3cr3t"))
    assert resolved == ResolvedToken("s3cr3t", "file")


def test_a_trailing_newline_from_echo_is_not_part_of_the_token(tmp_path: Path) -> None:
    """The ordinary way to write one of these is `echo secret > token`."""
    resolved = resolve_token(None, _token_file(tmp_path, "s3cr3t-value\n"))
    assert resolved.value == "s3cr3t-value"


def test_a_group_or_world_readable_token_file_is_refused(tmp_path: Path) -> None:
    """Mirrors the private-key assertion in tests/test_web_tls.py: a secret
    readable by more than its owner is not a secret. Refused rather than
    warned about, because a warning in a journal nobody reads is not a
    control."""
    path = _token_file(tmp_path, "s3cr3t", mode=0o644)
    with pytest.raises(TokenError) as caught:
        resolve_token(None, path)
    assert "chmod 600" in str(caught.value)


def test_an_empty_token_file_is_refused_rather_than_serving_unauthenticated(
    tmp_path: Path,
) -> None:
    """This is the failure that matters. `_authorised` in web/server.py treats
    a falsy token as "no token configured" and authorises every request, so an
    empty file would silently turn a Tailscale-reachable app into an open one.
    Refusing to start is the safe direction."""
    with pytest.raises(TokenError) as caught:
        resolve_token(None, _token_file(tmp_path, "   \n"))
    assert "empty" in str(caught.value)


def test_a_missing_token_file_is_refused_and_the_message_names_the_path(
    tmp_path: Path,
) -> None:
    with pytest.raises(TokenError) as caught:
        resolve_token(None, tmp_path / "absent")
    assert "absent" in str(caught.value)


@pytest.mark.parametrize("value", ["has a space", "sl/ash", "pl+us", "eq=uals", "עברית"])
def test_a_token_that_cannot_survive_a_url_is_refused(tmp_path: Path, value: str) -> None:
    """The token is delivered as `?token=...`, so the URL-unreserved set is
    the real constraint -- including the base64 padding and slash characters
    that a careless `openssl rand -base64` would produce."""
    with pytest.raises(TokenError):
        resolve_token(None, _token_file(tmp_path, value))


def test_a_file_token_is_redacted_in_the_printed_url(tmp_path: Path) -> None:
    """Pins the startup banner: under systemd it is captured by journald, and
    a token printed there outlives every rotation of the phone's bookmark."""
    resolved = resolve_token(None, _token_file(tmp_path, "s3cr3t"))
    assert "s3cr3t" not in token_query_suffix(resolved)
    assert token_query_suffix(resolved) == ""


def test_token_and_token_file_together_are_rejected(tmp_path: Path) -> None:
    """Two sources of truth for one credential is a configuration error, not
    a precedence puzzle to resolve silently."""
    with pytest.raises(TokenError):
        resolve_token("literal", _token_file(tmp_path, "s3cr3t"))


def test_token_auto_still_mints_a_token_and_still_prints_it() -> None:
    """Unchanged existing behaviour. `--token auto` is what the current field
    documentation tells the operator to use, and it prints the URL it minted
    because that is the only way to learn it."""
    first = resolve_token("auto", None)
    second = resolve_token("auto", None)
    assert first.source == "generated"
    assert first.value != second.value
    assert token_query_suffix(first) == f"?token={first.value}"


def test_an_explicit_token_is_used_verbatim_and_printed() -> None:
    """Unchanged existing behaviour."""
    resolved = resolve_token("hunter2", None)
    assert resolved == ResolvedToken("hunter2", "flag")
    assert token_query_suffix(resolved) == "?token=hunter2"


def test_no_token_at_all_still_yields_an_empty_suffix() -> None:
    """Unchanged existing behaviour: the app may be served without a token,
    and then the URL carries no query string."""
    resolved = resolve_token(None, None)
    assert resolved == ResolvedToken(None, "none")
    assert token_query_suffix(resolved) == ""


def test_web_serve_accepts_a_token_file_option_that_defaults_to_off() -> None:
    """Additive: the new option exists and changes nothing unless passed."""
    parameter = inspect.signature(web_serve).parameters["token_file"]
    assert parameter.default is None


def test_an_empty_token_flag_still_prints_a_bare_url() -> None:
    """Unchanged existing behaviour, and an easy one to lose: `--token ""`
    resolves to an empty string, which the server treats as "no token
    configured". The printed URL carried no query string before this helper
    existed, and must not start carrying `?token=` now."""
    assert token_query_suffix(resolve_token("", None)) == ""
