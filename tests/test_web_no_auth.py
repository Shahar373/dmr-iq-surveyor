"""`web serve --no-auth`: a field app with no token, made deliberately.

What is pinned here is what keeps "no token" from meaning "anyone": the flag
is refused unless the server listens on a Tailscale or loopback address, it
cannot be combined with a token, and the server then answers only requests
addressed to its own names -- so a page on another domain that resolves to
the Pi (DNS rebinding) is not served. A server started without the flag is
unchanged: no host check at all.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest
from fixtures.device_probe import StubProbeRunner
from fixtures.geo_scenario import build_database
from fixtures.live_profiles import write_profiles
from typer.testing import CliRunner

from dmr_iq_surveyor.cli_app import app
from dmr_iq_surveyor.cli_web import allowed_request_hosts, no_auth_refusal
from dmr_iq_surveyor.web.server import create_server, request_host
from dmr_iq_surveyor.web.service import FieldSettings

runner = CliRunner()


# -- where a server with no token may listen --------------------------------------


@pytest.mark.parametrize("host", ["100.90.110.54", "100.64.0.1", "100.127.255.254",
                                  "127.0.0.1", "::1", "fd7a:115c:a1e0::1"])
def test_tailscale_and_loopback_addresses_may_run_without_a_token(host: str) -> None:
    assert no_auth_refusal(host, token=None, token_file=None) == ""


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.20", "10.0.0.5", "100.128.0.1",
                                  "8.8.8.8", "raspberrypi.local"])
def test_any_other_address_keeps_its_token(host: str) -> None:
    assert no_auth_refusal(host, token=None, token_file=None)


def test_no_auth_and_a_token_together_are_refused() -> None:
    assert "cannot be combined" in no_auth_refusal("100.90.110.54", token="x", token_file=None)
    assert "cannot be combined" in no_auth_refusal(
        "100.90.110.54", token=None, token_file=Path("/etc/dmr-field/token")
    )


def test_the_answered_names_are_the_address_and_the_certificate_names() -> None:
    class _Certificate:
        hosts = ("raspberrypi-1.tail17e2fe.ts.net", "100.90.110.54", "LOCALHOST")

    assert allowed_request_hosts("100.90.110.54", _Certificate()) == (
        "100.90.110.54", "localhost", "raspberrypi-1.tail17e2fe.ts.net"
    )
    assert allowed_request_hosts("127.0.0.1", None) == ("127.0.0.1", "::1", "localhost")


@pytest.mark.parametrize(("header", "host"), [
    ("100.90.110.54:8765", "100.90.110.54"),
    ("Pi.Example.:8765", "pi.example"),
    ("[::1]:8765", "::1"),
    ("localhost", "localhost"),
    ("", ""),
])
def test_the_host_header_is_compared_without_port_case_or_brackets(header: str, host: str) -> None:
    assert request_host(header) == host


# -- the command ------------------------------------------------------------------


def _serve(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *extra: str) -> tuple[Any, list]:
    started: list = []
    monkeypatch.setattr(
        "dmr_iq_surveyor.cli_web.serve_forever",
        lambda settings, **kwargs: started.append((settings, kwargs)),
    )
    band, site = write_profiles(tmp_path / "profiles", center_hz=469_250_000.0)
    build_database(tmp_path / "db.sqlite3").close()
    result = runner.invoke(app, [
        "web", "serve", "--port", "0", "--band", str(band), "--site", str(site),
        "--database", str(tmp_path / "db.sqlite3"), "--output", str(tmp_path / "out"),
        "--center-frequency", "469250000", "--sample-rate", "2000000", *extra,
    ])
    return result, started


def test_web_serve_no_auth_on_a_lan_address_refuses_with_a_usage_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exit 2: the service unit does not restart on it, since retrying the
    same command cannot help."""
    result, started = _serve(tmp_path, monkeypatch, "--host", "192.168.1.20", "--no-auth")
    assert result.exit_code == 2, result.output
    assert "neither a Tailscale address nor loopback" in " ".join(result.output.split())
    assert started == []


def test_web_serve_no_auth_on_loopback_serves_with_no_token_and_a_host_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, started = _serve(tmp_path, monkeypatch, "--host", "127.0.0.1", "--no-auth")
    assert result.exit_code == 0, result.output
    settings = started[0][0]
    assert settings.token is None
    assert settings.allowed_request_hosts == ("127.0.0.1", "::1", "localhost")
    assert "No token (--no-auth)" in " ".join(result.output.split())


def test_web_serve_without_the_flag_has_no_host_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, started = _serve(tmp_path, monkeypatch, "--host", "127.0.0.1", "--token", "s3cret")
    assert result.exit_code == 0, result.output
    assert started[0][0].allowed_request_hosts is None


# -- the server ---------------------------------------------------------------------


def _request(port: int, path: str, *, host: str | None = None,
             body: dict | None = None) -> tuple[int, dict | str]:
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        method="POST" if body is not None else "GET",
        data=json.dumps(body).encode() if body is not None else None,
    )
    if host is not None:
        request.add_header("Host", host)
    if body is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            text = response.read().decode()
            status = response.status
    except urllib.error.HTTPError as error:
        text = error.read().decode()
        status = error.code
    try:
        return status, json.loads(text)
    except ValueError:
        return status, text


@pytest.fixture()
def tokenless(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("dmr_iq_surveyor.web.service.default_probe_runner", StubProbeRunner)
    build_database(tmp_path / "db.sqlite3").close()
    settings = FieldSettings(
        database_path=tmp_path / "db.sqlite3", output_root=tmp_path / "out",
        recordings_dir=tmp_path / "rec", token=None,
        allowed_request_hosts=("127.0.0.1", "::1", "localhost"),
    )
    server = create_server(settings, host="127.0.0.1", port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()


def test_a_tokenless_server_answers_its_own_address_without_a_token(tokenless: int) -> None:
    status, state = _request(tokenless, "/api/state")
    assert status == 200 and "settings" in state
    assert _request(tokenless, "/api/state", host=f"localhost:{tokenless}")[0] == 200
    status, page = _request(tokenless, "/locate")
    assert status == 200 and "dir=\"rtl\"" in page


def test_a_tokenless_server_refuses_a_request_addressed_to_another_name(tokenless: int) -> None:
    """DNS rebinding: evil.example resolves to the Pi, so the browser thinks
    the page and the API share an origin. The Host header still says
    evil.example, and that is what is refused -- reads, writes and pages."""
    for path in ("/api/state", "/locate", "/"):
        status, answer = _request(tokenless, path, host="evil.example")
        assert status == 421, (path, answer)
    status, answer = _request(
        tokenless, "/api/locate/target", host="evil.example", body={"frequency_mhz": 469.35}
    )
    assert status == 421
    assert "own address" in answer["error"]


def test_a_long_form_ipv6_bind_address_matches_what_a_browser_sends() -> None:
    """Browsers send the canonical, compressed literal; the bind address may
    have been typed long. Both sides are compared canonical."""
    allowed = allowed_request_hosts("fd7a:115c:a1e0:0:0:0:0:1", None)
    assert request_host("[fd7a:115c:a1e0::1]:8765") in allowed
    assert request_host("[FD7A:115C:A1E0:0:0:0:0:1]:8765") == "fd7a:115c:a1e0::1"
