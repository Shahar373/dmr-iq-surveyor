"""TLS for the field app: the certificate, and the server that presents it.

This is not a hardening exercise. Browsers gate `navigator.geolocation`
behind a secure context, so without TLS the phone will not share GPS and a
live drive cannot exist at all. These tests pin the properties that make
that work in a car park: the certificate names the addresses the phone will
actually type, it is not reissued behind the operator's back, and the server
really speaks TLS on the socket.
"""

from __future__ import annotations

import json
import ssl
import threading
import urllib.request
from pathlib import Path

import pytest

from dmr_iq_surveyor.web.server import create_server
from dmr_iq_surveyor.web.service import FieldSettings
from dmr_iq_surveyor.web.tls import (
    TlsUnavailable,
    ensure_self_signed,
    load_certificate,
)


def test_a_certificate_names_loopback_and_the_addresses_asked_for(tmp_path: Path) -> None:
    certificate = ensure_self_signed(tmp_path / "tls", hosts=["10.1.2.3", "pi.local"])
    assert certificate.generated
    # Loopback is always present: the operator tests from the Pi itself
    # before ever reaching it from the phone.
    assert {"localhost", "127.0.0.1"} <= set(certificate.hosts)
    assert {"10.1.2.3", "pi.local"} <= set(certificate.hosts)
    assert certificate.fingerprint_sha256 != "unknown"
    assert certificate.key_path.stat().st_mode & 0o077 == 0, "the private key must not be readable"


def test_an_existing_certificate_is_reused_rather_than_reissued(tmp_path: Path) -> None:
    """Reissuing changes the fingerprint, and every phone that had accepted
    the old one is sent back to the browser's warning page."""
    first = ensure_self_signed(tmp_path / "tls", hosts=["10.1.2.3"])
    second = ensure_self_signed(tmp_path / "tls", hosts=["10.1.2.3"])
    assert not second.generated
    assert second.fingerprint_sha256 == first.fingerprint_sha256


def test_a_certificate_missing_a_host_is_replaced(tmp_path: Path) -> None:
    """The Pi moved to another network. A certificate that does not name the
    address the phone will use is not a certificate the phone can accept."""
    first = ensure_self_signed(tmp_path / "tls", hosts=["10.1.2.3"])
    second = ensure_self_signed(tmp_path / "tls", hosts=["10.1.2.3", "192.168.9.9"])
    assert second.generated
    assert second.fingerprint_sha256 != first.fingerprint_sha256
    assert "192.168.9.9" in second.hosts


def test_loading_a_supplied_pair_reads_what_is_in_the_file(tmp_path: Path) -> None:
    issued = ensure_self_signed(tmp_path / "tls", hosts=["pi.local"])
    loaded = load_certificate(issued.certificate_path, issued.key_path)
    assert not loaded.generated
    assert loaded.fingerprint_sha256 == issued.fingerprint_sha256
    assert "pi.local" in loaded.hosts


def test_a_missing_supplied_certificate_says_so(tmp_path: Path) -> None:
    with pytest.raises(TlsUnavailable, match="not found"):
        load_certificate(tmp_path / "nope.crt", tmp_path / "nope.key")


def test_the_server_serves_the_api_over_tls(tmp_path: Path) -> None:
    certificate = ensure_self_signed(tmp_path / "tls")
    settings = FieldSettings(
        database_path=tmp_path / "db.sqlite3",
        output_root=tmp_path / "out",
        recordings_dir=tmp_path / "rec",
        token="s3cret",
    )
    server = create_server(settings, host="127.0.0.1", port=0, certificate=certificate)
    assert server.scheme == "https"
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    try:
        # Verified against the certificate itself, which is what installing it
        # on the phone amounts to: if this fails, "Advanced -> Proceed" is the
        # only way in and the certificate cannot be trusted permanently.
        context = ssl.create_default_context(cafile=str(certificate.certificate_path))
        request = urllib.request.Request(f"https://localhost:{port}/api/state")
        request.add_header("X-Auth-Token", "s3cret")
        with urllib.request.urlopen(request, context=context, timeout=30) as response:
            payload = json.loads(response.read().decode())
        assert "settings" in payload

        # A plain-HTTP request to the TLS port is a mistake an operator makes
        # constantly. It must fail as a failed request, not take the server
        # down: the next HTTPS request has to still work.
        with pytest.raises(Exception):  # noqa: B017,PT011 - any transport failure will do
            urllib.request.urlopen(f"http://127.0.0.1:{port}/api/state", timeout=10)
        with urllib.request.urlopen(request, context=context, timeout=30) as response:
            assert response.status == 200
    finally:
        server.shutdown()
        server.server_close()
