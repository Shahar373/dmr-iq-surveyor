"""The map-tile proxy: OSM blocks Referer-less browser requests, so the Pi fetches.

The app sends `Referrer-Policy: no-referrer` (the token is in the page URL),
and OSM answered every tile with a 403 image. These pin that tiles now come
through the Pi with an identifying User-Agent, are cached, survive an
upstream outage when cached, and are not an open relay.
"""

from __future__ import annotations

import http.client
import threading
from pathlib import Path

import pytest
from fixtures.device_probe import StubProbeRunner, present
from fixtures.geo_scenario import build_database

from dmr_iq_surveyor.web.server import create_server
from dmr_iq_surveyor.web.service import FieldSettings
from dmr_iq_surveyor.web.tiles import FRESH_SECONDS, USER_AGENT, TileCache, TileError, proxiable

PNG = b"\x89PNG\r\n\x1a\nfake"


class _Upstream:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.fail = False

    def __call__(self, url: str, timeout: float) -> bytes:
        self.calls.append(url)
        if self.fail:
            raise OSError("no route to host")
        return PNG


def test_cache_fetches_once_and_fills_the_template(tmp_path: Path) -> None:
    upstream = _Upstream()
    cache = TileCache(tmp_path, "https://tile.example/{z}/{x}/{y}.png", fetch=upstream)
    assert cache.get(3, 4, 5) == PNG
    assert cache.get(3, 4, 5) == PNG
    assert upstream.calls == ["https://tile.example/3/4/5.png"], "second request is served from disk"
    assert (tmp_path / "3" / "4" / "5.png").read_bytes() == PNG


def test_stale_tile_is_served_when_upstream_is_down(tmp_path: Path) -> None:
    now = [1_000_000.0]
    upstream = _Upstream()
    cache = TileCache(tmp_path, "https://t/{z}/{x}/{y}.png", fetch=upstream, clock=lambda: now[0])
    cache.get(1, 0, 0)
    now[0] += FRESH_SECONDS * 10
    upstream.fail = True
    assert cache.get(1, 0, 0) == PNG, "offline in the field: an old tile beats none"


def test_uncached_tile_with_upstream_down_is_a_502(tmp_path: Path) -> None:
    upstream = _Upstream()
    upstream.fail = True
    cache = TileCache(tmp_path, "https://t/{z}/{x}/{y}.png", fetch=upstream)
    with pytest.raises(TileError) as caught:
        cache.get(2, 1, 1)
    assert caught.value.status == 502


@pytest.mark.parametrize(("z", "x", "y"), [(-1, 0, 0), (20, 0, 0), (2, 4, 0), (2, 0, -1)])
def test_out_of_range_tiles_are_refused_without_fetching(tmp_path: Path, z, x, y) -> None:
    upstream = _Upstream()
    cache = TileCache(tmp_path, "https://t/{z}/{x}/{y}.png", fetch=upstream)
    with pytest.raises(TileError):
        cache.get(z, x, y)
    assert upstream.calls == []


def test_prune_keeps_the_cache_under_its_cap(tmp_path: Path) -> None:
    cache = TileCache(tmp_path, "https://t/{z}/{x}/{y}.png", fetch=_Upstream(), max_bytes=40)
    for x in range(8):
        cache.get(3, x, 0)
    cache.prune()
    total = sum(f.stat().st_size for f in tmp_path.rglob("*.png"))
    assert total <= 40


def test_only_http_templates_are_proxied() -> None:
    assert proxiable("https://tile.openstreetmap.org/{z}/{x}/{y}.png")
    assert not proxiable("/local/tiles/{z}/{x}/{y}.png")
    assert "dmr-iq-surveyor" in USER_AGENT and "urllib" not in USER_AGENT.lower()


def _server(tmp_path: Path, **overrides):
    database = tmp_path / "db.sqlite3"
    build_database(database).close()
    settings = FieldSettings(database_path=database, output_root=tmp_path / "out",
                             recordings_dir=tmp_path / "rec", token="s3cret", **overrides)
    server = create_server(settings, host="127.0.0.1", port=0,
                           probe_runner=StubProbeRunner(present("fake SDR")))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _get(server, path: str):
    connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
    connection.request("GET", path)
    response = connection.getresponse()
    body = response.read()
    connection.close()
    return response, body


def test_tiles_are_served_through_the_pi_and_need_the_token(tmp_path: Path) -> None:
    server = _server(tmp_path)
    upstream = _Upstream()
    server.service.tile_cache._fetch = upstream
    try:
        response, _ = _get(server, "/tiles/3/4/5.png")
        assert response.status == 401, "not an open relay"
        response, body = _get(server, "/tiles/3/4/5.png?token=s3cret")
        assert response.status == 200 and body == PNG
        assert response.getheader("Content-Type") == "image/png"
        assert response.getheader("Referrer-Policy") == "no-referrer"
        response, _ = _get(server, "/tiles/3/x/5.png?token=s3cret")
        assert response.status == 404
        assert server.service.state()["tiles_via_pi"] is True
    finally:
        server.shutdown()
        server.server_close()
        server.service.close()


def test_no_tile_proxy_keeps_direct_fetching(tmp_path: Path) -> None:
    server = _server(tmp_path, tile_proxy=False)
    try:
        assert server.service.tile_cache is None
        assert server.service.state()["tiles_via_pi"] is False
        response, _ = _get(server, "/tiles/3/4/5.png?token=s3cret")
        assert response.status == 404
    finally:
        server.shutdown()
        server.server_close()
        server.service.close()
