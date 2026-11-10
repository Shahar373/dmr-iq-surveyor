"""A caching map-tile proxy on the Pi.

Why it exists: the field app sends `Referrer-Policy: no-referrer` on every
response, so the access token in the page URL can never leak to a third
party (see `server.FieldRequestHandler.end_headers`). OpenStreetMap's tile
servers refuse browser requests that arrive with no `Referer` -- every tile
came back as a 403 "Access blocked" image. Sending a referrer would put the
token-bearing origin back on the wire; proxying does not.

So the phone asks the Pi (`/tiles/{z}/{x}/{y}.png`, same origin, token
checked like any API call), and the Pi fetches from the upstream template
with an identifying User-Agent, as the OSM tile usage policy asks, and keeps
a disk cache. That cache is also what makes the map usable in the field
without internet: tiles seen once at home are served from disk later.

Bounded on purpose: two upstream fetches at a time (the policy's limit), one
fetch per tile however many requests ask for it concurrently, a size cap on
the cache, and a stale tile is served rather than none when upstream fails.
"""

from __future__ import annotations

import os
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path

from dmr_iq_surveyor import __version__

MAX_ZOOM = 19
# OSM asks clients to honour cache headers; tiles change rarely and a week is
# what its own servers advertise for most tiles.
FRESH_SECONDS = 7 * 24 * 3600
DEFAULT_MAX_BYTES = 500 * 1024 * 1024
USER_AGENT = f"dmr-iq-surveyor/{__version__} (field app tile cache)"
_UPSTREAM_SLOTS = 2
_PRUNE_EVERY = 200  # stores between size checks


class TileError(Exception):
    """A tile that could not be served; `status` is the HTTP status to answer."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def proxiable(template: str) -> bool:
    """Only an http(s) template can be fetched by the Pi on the phone's behalf."""
    return template.startswith(("http://", "https://"))


def _default_fetch(url: str, timeout: float) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        return response.read()


class TileCache:
    def __init__(
        self,
        root: Path,
        template: str,
        *,
        fetch: Callable[[str, float], bytes] = _default_fetch,
        max_bytes: int = DEFAULT_MAX_BYTES,
        timeout: float = 10.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.root = Path(root)
        self.template = template
        self._fetch = fetch
        self._max_bytes = max_bytes
        self._timeout = timeout
        self._clock = clock
        self._slots = threading.BoundedSemaphore(_UPSTREAM_SLOTS)
        self._locks: dict[tuple[int, int, int], threading.Lock] = {}
        self._locks_guard = threading.Lock()
        self._stores = 0

    def _path(self, z: int, x: int, y: int) -> Path:
        return self.root / str(z) / str(x) / f"{y}.png"

    def _url(self, z: int, x: int, y: int) -> str:
        return (
            self.template.replace("{s}", "a")
            .replace("{z}", str(z))
            .replace("{x}", str(x))
            .replace("{y}", str(y))
            .replace("{r}", "")
        )

    def _lock_for(self, key: tuple[int, int, int]) -> threading.Lock:
        with self._locks_guard:
            lock = self._locks.get(key)
            if lock is None:
                if len(self._locks) > 4096:
                    self._locks.clear()
                lock = self._locks[key] = threading.Lock()
            return lock

    def get(self, z: int, x: int, y: int) -> bytes:
        if not 0 <= z <= MAX_ZOOM or not (0 <= x < 2**z and 0 <= y < 2**z):
            raise TileError(404, f"no such tile {z}/{x}/{y}")
        path = self._path(z, x, y)
        with self._lock_for((z, x, y)):
            stale: bytes | None = None
            try:
                age = self._clock() - path.stat().st_mtime
                body = path.read_bytes()
                if age < FRESH_SECONDS:
                    return body
                stale = body
            except OSError:
                pass
            try:
                with self._slots:
                    body = self._fetch(self._url(z, x, y), self._timeout)
            except (OSError, urllib.error.URLError) as exc:
                if stale is not None:
                    return stale  # offline in the field: an old tile beats none
                raise TileError(502, f"tile upstream unavailable: {exc}") from exc
            if not body:
                if stale is not None:
                    return stale
                raise TileError(502, "tile upstream returned an empty body")
            self._store(path, body)
            return body

    def _store(self, path: Path, body: bytes) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_bytes(body)
            os.replace(tmp, path)
        except OSError:
            return  # a full disk costs the cache, never the tile
        self._stores += 1
        if self._stores % _PRUNE_EVERY == 0:
            self.prune()

    def prune(self) -> None:
        """Drop the least recently stored tiles once the cache exceeds its cap."""
        entries = []
        total = 0
        for file in self.root.rglob("*.png"):
            try:
                stat = file.stat()
            except OSError:
                continue
            entries.append((stat.st_mtime, stat.st_size, file))
            total += stat.st_size
        if total <= self._max_bytes:
            return
        target = int(self._max_bytes * 0.8)
        for _, size, file in sorted(entries):
            try:
                file.unlink()
            except OSError:
                continue
            total -= size
            if total <= target:
                break


__all__ = ["TileCache", "TileError", "USER_AGENT", "proxiable"]
