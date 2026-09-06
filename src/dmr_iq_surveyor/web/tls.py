"""A self-signed certificate for the field app, because the browser insists.

`navigator.geolocation` is gated behind a *secure context*. Served over plain
HTTP to anything other than localhost, a phone does not merely warn -- it
refuses to hand the page a fix at all. The live survey is built entirely on
the phone's GPS, so without TLS the feature cannot exist. That is the whole
reason this module is here; it is not a security posture for an
internet-facing service, which this never becomes.

What it produces is a certificate the operator has to accept once per device
(Chrome: "Advanced" -> "Proceed"), after which the origin counts as secure
and geolocation works. It can also be installed as a trusted certificate on
the phone, which is why the default lifetime stays under the 398 days
browsers will accept for a leaf certificate.

openssl rather than a Python library: it is already on any Raspberry Pi that
can reach an APT mirror, and adding a build-heavy dependency to a tool that
has to install on a Pi in a car park is exactly the failure this project's
web server was written to avoid. If openssl is somehow absent, the error says
so and points at `--tls-cert/--tls-key`.
"""

from __future__ import annotations

import ipaddress
import re
import shutil
import socket
import ssl
import subprocess
from dataclasses import dataclass
from pathlib import Path

# Under the 398 days browsers enforce for leaf certificates, so the same file
# can be installed as trusted on a phone instead of being clicked through at
# every launch. It is re-issued automatically as it nears expiry.
DEFAULT_VALIDITY_DAYS = 397
# Re-issued this far ahead of expiry, so a campaign never begins with a
# certificate that dies mid-drive.
RENEW_WITHIN_DAYS = 14
# A handshake that never completes must not hold the accept loop. Ten seconds
# is far beyond any real client and far below "forever".
HANDSHAKE_TIMEOUT_SECONDS = 10.0

_CERT_NAME = "field-app.crt"
_KEY_NAME = "field-app.key"


class TlsUnavailable(RuntimeError):
    """openssl is missing, or refused to issue the certificate."""


@dataclass(frozen=True, slots=True)
class Certificate:
    certificate_path: Path
    key_path: Path
    hosts: tuple[str, ...]
    fingerprint_sha256: str
    not_after: str
    # True when this run created it. Reported rather than inferred: an
    # operator who has already trusted a certificate on a phone needs to know
    # when it has been replaced underneath them.
    generated: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "certificate_path": str(self.certificate_path),
            "key_path": str(self.key_path),
            "hosts": list(self.hosts),
            "fingerprint_sha256": self.fingerprint_sha256,
            "not_after": self.not_after,
            "generated": self.generated,
        }


def local_ip_addresses() -> list[str]:
    """Addresses a phone on the same network could actually use.

    The routed source address first, found the same way the app's printed
    URLs are found -- a hostname on a Pi tethered to a hotspot usually
    resolves to 127.0.1.1, which is precisely the address that will not work
    from the phone, and a certificate naming only that is a certificate for
    nowhere.
    """
    found: list[str] = []
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("192.0.2.1", 1))  # TEST-NET-1: routed nowhere, sends nothing
        found.append(probe.getsockname()[0])
    except OSError:
        pass
    finally:
        probe.close()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            address = info[4][0]
            if address not in found and not address.startswith("127."):
                found.append(address)
    except OSError:
        pass
    return found


def _openssl() -> str:
    executable = shutil.which("openssl")
    if executable is None:
        raise TlsUnavailable(
            "openssl was not found, so a certificate cannot be issued. Install it "
            "(apt install openssl), or supply your own with --tls-cert and --tls-key."
        )
    return executable


def _run(arguments: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed executable, no shell
        arguments, capture_output=True, text=True, check=False, timeout=120
    )


def _split_hosts(hosts: list[str]) -> tuple[list[str], list[str]]:
    """(DNS names, IP addresses), in the order given, without duplicates."""
    names: list[str] = []
    addresses: list[str] = []
    for host in hosts:
        candidate = host.strip()
        if not candidate:
            continue
        try:
            ipaddress.ip_address(candidate)
        except ValueError:
            if candidate not in names:
                names.append(candidate)
        else:
            if candidate not in addresses:
                addresses.append(candidate)
    return names, addresses


def _canonical(host: str) -> str:
    """One spelling for a host, so a match is a match.

    openssl prints IPv6 SANs expanded (`0:0:0:0:0:0:0:1` for `::1`), so a
    literal string comparison decided the certificate was missing a host it
    already had -- and reissued it on every launch, sending every phone that
    had trusted the old one back to the warning page.
    """
    candidate = host.strip()
    try:
        return str(ipaddress.ip_address(candidate))
    except ValueError:
        return candidate.lower()


def _subject_alt_names(certificate_path: Path) -> set[str]:
    """The SANs actually in the file, read from the file.

    Read back rather than remembered in a sidecar: a certificate that was
    replaced, truncated or copied from another machine must be judged on what
    it contains, not on what this program once wrote down about it.
    """
    result = _run([_openssl(), "x509", "-in", str(certificate_path), "-noout", "-ext",
                   "subjectAltName"])
    if result.returncode != 0:
        return set()
    return {
        _canonical(value)
        for value in re.findall(r"(?:DNS|IP Address):([^,\s]+)", result.stdout)
    }


def _fingerprint(certificate_path: Path) -> str:
    result = _run([_openssl(), "x509", "-in", str(certificate_path), "-noout",
                   "-fingerprint", "-sha256"])
    if result.returncode != 0:
        return "unknown"
    _, _, value = result.stdout.strip().partition("=")
    return value.strip() or "unknown"


def _not_after(certificate_path: Path) -> str:
    result = _run([_openssl(), "x509", "-in", str(certificate_path), "-noout", "-enddate"])
    if result.returncode != 0:
        return "unknown"
    _, _, value = result.stdout.strip().partition("=")
    return value.strip() or "unknown"


def _still_valid(certificate_path: Path, *, renew_within_days: int) -> bool:
    result = _run([_openssl(), "x509", "-in", str(certificate_path), "-noout",
                   "-checkend", str(renew_within_days * 86_400)])
    return result.returncode == 0


def ensure_self_signed(
    directory: str | Path,
    *,
    hosts: list[str] | None = None,
    days: int = DEFAULT_VALIDITY_DAYS,
    renew_within_days: int = RENEW_WITHIN_DAYS,
) -> Certificate:
    """Return a usable certificate/key pair, issuing one only if needed.

    Kept rather than reissued whenever the existing pair still covers every
    requested host and is not near expiry: reissuing changes the fingerprint,
    and every phone that had accepted the old one is sent back to the warning
    page. A certificate is replaced only when it would not work.
    """
    target = Path(directory).expanduser().resolve()
    certificate_path = target / _CERT_NAME
    key_path = target / _KEY_NAME
    requested = ["localhost", "127.0.0.1", "::1", *(hosts or []), *local_ip_addresses()]
    names, addresses = _split_hosts(requested)
    wanted = {_canonical(host) for host in (*names, *addresses)}

    if certificate_path.is_file() and key_path.is_file():
        present = _subject_alt_names(certificate_path)
        if wanted <= present and _still_valid(certificate_path, renew_within_days=renew_within_days):
            return Certificate(
                certificate_path=certificate_path,
                key_path=key_path,
                hosts=tuple(sorted(present)),
                fingerprint_sha256=_fingerprint(certificate_path),
                not_after=_not_after(certificate_path),
                generated=False,
            )

    target.mkdir(parents=True, exist_ok=True)
    alt = ",".join([*(f"DNS:{name}" for name in names), *(f"IP:{ip}" for ip in addresses)])
    result = _run(
        [
            _openssl(), "req", "-x509",
            # P-256 rather than RSA: a Pi issues it in milliseconds instead of
            # seconds, and every browser that matters has supported it for a
            # decade.
            "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1",
            "-sha256", "-days", str(days), "-nodes",
            "-keyout", str(key_path), "-out", str(certificate_path),
            "-subj", "/CN=dmr-iq-surveyor field app",
            "-addext", f"subjectAltName={alt}",
            "-addext", "basicConstraints=critical,CA:FALSE",
            "-addext", "keyUsage=critical,digitalSignature,keyEncipherment",
            "-addext", "extendedKeyUsage=serverAuth",
        ]
    )
    if result.returncode != 0 or not certificate_path.is_file():
        raise TlsUnavailable(
            "openssl could not issue a certificate: "
            f"{(result.stderr or result.stdout).strip() or 'no output'}"
        )
    # The private key is readable by its owner only. It is written into a
    # campaign output directory that also holds recordings and reports, which
    # are not otherwise sensitive, so the mode has to be set explicitly.
    key_path.chmod(0o600)
    return Certificate(
        certificate_path=certificate_path,
        key_path=key_path,
        hosts=tuple(sorted(wanted)),
        fingerprint_sha256=_fingerprint(certificate_path),
        not_after=_not_after(certificate_path),
        generated=True,
    )


def load_certificate(certificate_path: str | Path, key_path: str | Path) -> Certificate:
    """Describe an operator-supplied pair, without reissuing anything."""
    certificate = Path(certificate_path).expanduser().resolve()
    key = Path(key_path).expanduser().resolve()
    for path in (certificate, key):
        if not path.is_file():
            raise TlsUnavailable(f"TLS file not found: {path}")
    return Certificate(
        certificate_path=certificate,
        key_path=key,
        hosts=tuple(sorted(_subject_alt_names(certificate))),
        fingerprint_sha256=_fingerprint(certificate),
        not_after=_not_after(certificate),
        generated=False,
    )


def build_context(certificate: Certificate) -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    try:
        context.load_cert_chain(certificate.certificate_path, certificate.key_path)
    except (ssl.SSLError, OSError) as exc:
        raise TlsUnavailable(
            f"the certificate and key could not be loaded together: {exc}"
        ) from exc
    return context


__all__ = [
    "DEFAULT_VALIDITY_DAYS",
    "HANDSHAKE_TIMEOUT_SECONDS",
    "RENEW_WITHIN_DAYS",
    "Certificate",
    "TlsUnavailable",
    "build_context",
    "ensure_self_signed",
    "load_certificate",
    "local_ip_addresses",
]
