"""Every response carries `Referrer-Policy: no-referrer`.

The shared token reaches the browser in the page's own URL (`?token=...`),
because static assets are served unauthenticated so that `app.js` can load and
read it out of `location.search`. It therefore stays in `document.location`
for the life of the tab, and a browser's default referrer policy puts the
referring URL into the `Referer` header of cross-origin subresource requests.
The shipped `--tile-url` points at `tile.openstreetmap.org`, so without this
header a field session hands its API token to a third-party tile server a few
hundred times over -- once per tile.

These tests go over a real socket rather than calling the handler directly,
because the header is emitted from `end_headers()` and the thing worth pinning
is that *every* response path reaches it: JSON, static files, an
unauthenticated 401, and the base class's own error responses.
"""

from __future__ import annotations

import threading
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import pytest
from fixtures.device_probe import StubProbeRunner

from dmr_iq_surveyor.web.server import create_server
from dmr_iq_surveyor.web.service import FieldSettings

TOKEN = "s3cret-token"


@pytest.fixture(autouse=True)
def _no_real_sdr(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test here depends on hardware; substitute the probe factory so the
    result is the same on a laptop and on a Pi with an RSP1A attached."""
    monkeypatch.setattr(
        "dmr_iq_surveyor.web.service.default_probe_runner",
        StubProbeRunner,
    )


@pytest.fixture()
def base_url(tmp_path: Path) -> Iterator[str]:
    settings = FieldSettings(
        database_path=tmp_path / "db.sqlite3",
        output_root=tmp_path / "out",
        recordings_dir=tmp_path / "rec",
        token=TOKEN,
    )
    server = create_server(settings, host="127.0.0.1", port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def _headers(url: str) -> dict[str, str]:
    """Fetch `url`, returning the response headers whether it succeeded or not."""
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            return dict(response.headers)
    except urllib.error.HTTPError as error:
        error.read()
        return dict(error.headers)


@pytest.mark.parametrize(
    ("path", "what"),
    [
        ("/", "the page itself, which is where the token lands"),
        ("/app.js", "a static asset"),
        ("/api/state?token=" + TOKEN, "an authorised API response"),
        ("/api/state", "an unauthorised API response"),
        ("/api/nonexistent?token=" + TOKEN, "a 404 from the router"),
    ],
)
def test_every_response_forbids_sending_the_referrer(
    base_url: str, path: str, what: str
) -> None:
    assert _headers(base_url + path).get("Referrer-Policy") == "no-referrer", what


def test_the_header_is_present_exactly_once(base_url: str) -> None:
    """`end_headers()` is called once per response, but a future call site that
    also set the header by hand would produce a duplicate, and duplicated
    policy headers are how a browser ends up ignoring the value entirely."""
    with urllib.request.urlopen(base_url + "/", timeout=10) as response:
        assert response.headers.get_all("Referrer-Policy") == ["no-referrer"]


def test_the_token_is_still_accepted_from_the_query_string(base_url: str) -> None:
    """The header must not be mistaken for a fix to the underlying design: the
    token still travels in the URL, and this pins that the app kept working."""
    with urllib.request.urlopen(base_url + "/api/state?token=" + TOKEN, timeout=10) as ok:
        assert ok.status == 200
