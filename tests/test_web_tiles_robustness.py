"""The tile proxy under real map use: cancelled tiles, odd upstream failures, load.

Leaflet cancels in-flight tile requests on every pan and zoom, so a socket
closed mid-tile is routine and must not print a traceback. An upstream error
the cache did not anticipate must still answer JSON. The size cap must not
be enforced inside a tile request (a walk of a ~500 MB cache on an SD card
takes seconds). Concurrent requests for one tile must share one lock however
many other tiles pass through, and two writers must never share a temp file.
"""

from __future__ import annotations

import http.client
import json
import os
import socket
import struct
import threading
import time
from pathlib import Path

import pytest
from fixtures.device_probe import StubProbeRunner, present
from fixtures.geo_scenario import build_database

from dmr_iq_surveyor.web import tiles
from dmr_iq_surveyor.web.server import create_server
from dmr_iq_surveyor.web.service import FieldSettings
from dmr_iq_surveyor.web.tiles import FRESH_SECONDS, TileCache, TileError

PNG = b"\x89PNG\r\n\x1a\nfake"
TEMPLATE = "https://t/{z}/{x}/{y}.png"


def _wait_until(condition, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("condition not reached in time")
        time.sleep(0.005)


# -- server: tile requests share the API's exception handling ---------------


class _RecordingServer:
    """A live server that records tracebacks and the end of each request."""

    def __init__(self, tmp_path: Path) -> None:
        database = tmp_path / "db.sqlite3"
        build_database(database).close()
        settings = FieldSettings(database_path=database, output_root=tmp_path / "out",
                                 recordings_dir=tmp_path / "rec", token="s3cret")
        self.server = create_server(settings, host="127.0.0.1", port=0,
                                    probe_runner=StubProbeRunner(present("fake SDR")))
        self.errors: list[BaseException | None] = []
        self.finished = threading.Event()
        original_shutdown = self.server.shutdown_request

        def handle_error(request, client_address) -> None:  # socketserver's traceback hook
            import sys

            self.errors.append(sys.exc_info()[1])

        def shutdown_request(request) -> None:
            original_shutdown(request)
            self.finished.set()

        self.server.handle_error = handle_error
        self.server.shutdown_request = shutdown_request
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def cache(self) -> TileCache:
        return self.server.service.tile_cache

    @property
    def port(self) -> int:
        return self.server.server_address[1]

    def get(self, path: str):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        connection.request("GET", path)
        response = connection.getresponse()
        body = response.read()
        connection.close()
        return response, body

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.server.service.close()


@pytest.fixture
def live(tmp_path: Path):
    server = _RecordingServer(tmp_path)
    try:
        yield server
    finally:
        server.close()


def test_a_tile_cancelled_mid_response_closes_quietly(live: _RecordingServer) -> None:
    released = threading.Event()
    big = PNG + b"\0" * (8 * 1024 * 1024)  # far more than any socket buffer holds

    def fetch(url: str, timeout: float) -> bytes:
        released.wait(10)
        return big

    live.cache._fetch = fetch
    client = socket.create_connection(("127.0.0.1", live.port), timeout=10)
    client.sendall(b"GET /tiles/3/4/5.png?token=s3cret HTTP/1.1\r\nHost: x\r\n\r\n")
    _wait_until(lambda: live.cache._locks)  # the handler is inside cache.get
    # Close with a reset, as a browser aborting the request does.
    client.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    client.close()
    released.set()
    assert live.finished.wait(10), "the handler finished"
    assert live.errors == [], "a cancelled tile is routine, not a traceback"


def test_an_unexpected_tile_failure_is_a_502_json_error(live: _RecordingServer) -> None:
    def fetch(url: str, timeout: float) -> bytes:
        raise RuntimeError("something nobody planned for")

    live.cache._fetch = fetch
    response, body = live.get("/tiles/3/4/5.png?token=s3cret")
    assert response.status == 502
    assert response.getheader("Content-Type").startswith("application/json")
    assert "RuntimeError" in json.loads(body)["error"]
    assert live.errors == []
    live.cache._fetch = lambda url, timeout: PNG
    response, body = live.get("/tiles/3/4/5.png?token=s3cret")
    assert response.status == 200 and body == PNG, "the server carries on"


def test_a_truncated_upstream_body_is_a_502_not_an_escape(live: _RecordingServer) -> None:
    def fetch(url: str, timeout: float) -> bytes:
        raise http.client.IncompleteRead(b"\x89PNG", 1000)

    live.cache._fetch = fetch
    response, body = live.get("/tiles/3/4/5.png?token=s3cret")
    assert response.status == 502
    assert "error" in json.loads(body)
    assert live.errors == []


def test_a_truncated_upstream_body_falls_back_to_the_stale_tile(tmp_path: Path) -> None:
    now = [time.time()]  # tile age is clock minus the file's real mtime
    calls = []

    def fetch(url: str, timeout: float) -> bytes:
        calls.append(url)
        if len(calls) > 1:
            raise http.client.IncompleteRead(b"\x89PNG", 1000)
        return PNG

    cache = TileCache(tmp_path, TEMPLATE, fetch=fetch, clock=lambda: now[0])
    cache.get(2, 1, 1)
    now[0] += FRESH_SECONDS * 2
    assert cache.get(2, 1, 1) == PNG, "an old tile beats none, whatever broke upstream"
    assert len(calls) == 2


def test_a_stale_tile_really_asks_upstream_first(tmp_path: Path) -> None:
    # Tile age is the clock minus the file's *real* mtime, so a fake clock
    # near zero makes every tile look fresh and upstream is never asked.
    now = [time.time()]
    calls = []

    def fetch(url: str, timeout: float) -> bytes:
        calls.append(url)
        if len(calls) > 1:
            raise OSError("no route to host")
        return PNG

    cache = TileCache(tmp_path, TEMPLATE, fetch=fetch, clock=lambda: now[0])
    cache.get(1, 0, 0)
    now[0] += FRESH_SECONDS * 2
    assert cache.get(1, 0, 0) == PNG
    assert len(calls) == 2, "the stale path, not the fresh one, served it"


# -- cache: prune runs off the request path ---------------------------------


def test_prune_runs_in_the_background_not_in_the_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tiles, "_PRUNE_EVERY", 2)
    cache = TileCache(tmp_path, TEMPLATE, fetch=lambda url, timeout: PNG, max_bytes=40)
    started = threading.Event()
    release = threading.Event()
    runs: list[str] = []
    real_prune = cache._prune

    def slow_prune() -> None:
        runs.append(threading.current_thread().name)
        started.set()
        release.wait(10)
        real_prune()

    cache._prune = slow_prune
    request_thread = threading.current_thread().name
    for x in range(2):
        cache.get(3, x, 0)  # returns although the prune it started is blocked
    assert started.wait(5)
    for x in range(2, 6):
        cache.get(3, x, 0)  # two more due prunes while one runs: coalesced
    assert runs and request_thread not in runs
    release.set()
    cache._prune_thread.join(5)
    assert len(runs) == 1, "never two prunes at once"
    total = sum(f.stat().st_size for f in tmp_path.rglob("*.png"))
    assert total <= 40


def test_a_failing_background_prune_is_swallowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tiles, "_PRUNE_EVERY", 1)
    cache = TileCache(tmp_path, TEMPLATE, fetch=lambda url, timeout: PNG)
    failures = []

    def broken() -> None:
        failures.append(1)
        raise OSError("card pulled")

    cache._prune = broken
    hooked = []
    monkeypatch.setattr(threading, "excepthook", lambda args: hooked.append(args))
    assert cache.get(1, 0, 0) == PNG
    cache._prune_thread.join(5)
    assert failures == [1] and hooked == []


def test_every_store_is_counted_under_concurrency(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tiles, "_PRUNE_EVERY", 10)
    cache = TileCache(tmp_path, TEMPLATE, fetch=lambda url, timeout: PNG)
    triggered = []
    cache._prune_in_background = lambda: triggered.append(1)
    workers, each = 8, 25

    def work(w: int) -> None:
        for i in range(each):
            cache.get(10, w, i)

    threads = [threading.Thread(target=work, args=(w,)) for w in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    assert cache._stores == workers * each
    assert len(triggered) == workers * each // 10


# -- cache: per-tile locks are never dropped while held ---------------------


def test_one_fetch_per_tile_however_many_tiles_pass_through(tmp_path: Path) -> None:
    gate = threading.Event()
    calls: list[str] = []

    def fetch(url: str, timeout: float) -> bytes:
        calls.append(url)
        if url.endswith("/5/1/1.png"):
            gate.wait(10)
        return PNG

    cache = TileCache(tmp_path, TEMPLATE, fetch=fetch)
    first = threading.Thread(target=cache.get, args=(5, 1, 1))
    first.start()
    _wait_until(lambda: calls)  # the first request is fetching, holding the tile lock
    # More distinct tiles than the old table's 4096 cap, which used to clear
    # it -- the held lock included -- and let a second fetch of 5/1/1 start.
    for x in range(4200):
        cache.get(13, x, 0)
    second = threading.Thread(target=cache.get, args=(5, 1, 1))
    second.start()
    _wait_until(lambda: cache._locks.get((5, 1, 1), (None, 0))[1] == 2)
    gate.set()
    first.join(10)
    second.join(10)
    assert calls.count("https://t/5/1/1.png") == 1, "the second request was served from disk"
    assert cache._locks == {}, "the table holds only tiles in flight"


# -- cache: temp files are per writer ---------------------------------------


def test_each_writer_gets_its_own_temp_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = TileCache(tmp_path, TEMPLATE, fetch=lambda url, timeout: PNG)
    sources: list[str] = []
    real_replace = os.replace

    def replace(src, dst) -> None:
        sources.append(str(src))
        real_replace(src, dst)

    monkeypatch.setattr(tiles.os, "replace", replace)
    target = tmp_path / "3" / "4" / "5.png"
    cache._store(target, PNG)
    cache._store(target, PNG + b"2")
    assert len(set(sources)) == 2, "two writers of one tile never share a temp path"
    assert all(src.endswith(".tmp") and Path(src).parent == target.parent for src in sources)
    assert target.read_bytes() == PNG + b"2"
    assert list(tmp_path.rglob("*.tmp")) == []


def test_a_failed_write_leaves_no_temp_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = TileCache(tmp_path, TEMPLATE, fetch=lambda url, timeout: PNG)

    def replace(src, dst) -> None:
        raise OSError("No space left on device")

    monkeypatch.setattr(tiles.os, "replace", replace)
    assert cache.get(3, 4, 5) == PNG, "a full disk costs the cache, never the tile"
    assert list(tmp_path.rglob("*")) == [tmp_path / "3", tmp_path / "3" / "4"]


def test_prune_sweeps_orphaned_temp_files_only(tmp_path: Path) -> None:
    now = 1_000_000.0
    cache = TileCache(tmp_path, TEMPLATE, fetch=lambda url, timeout: PNG, clock=lambda: now)
    folder = tmp_path / "3" / "4"
    folder.mkdir(parents=True)
    orphan = folder / "5.dead.tmp"
    live_writer = folder / "6.busy.tmp"
    for file in (orphan, live_writer):
        file.write_bytes(PNG)
    os.utime(orphan, (now - 2 * 3600, now - 2 * 3600))
    os.utime(live_writer, (now - 5, now - 5))
    cache.prune()
    assert not orphan.exists()
    assert live_writer.exists()


def test_tile_errors_still_carry_their_status(tmp_path: Path) -> None:
    cache = TileCache(tmp_path, TEMPLATE, fetch=lambda url, timeout: b"")
    with pytest.raises(TileError) as caught:
        cache.get(1, 0, 0)
    assert caught.value.status == 502
