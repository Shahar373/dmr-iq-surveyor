"""A small standard-library HTTP server for the field web app.

Built on `http.server` rather than a web framework on purpose. This runs on
a Raspberry Pi in a car park, reached from a phone on the same hotspot; a
tool that fails because a dependency did not install is worse than one with
a hand-written router. The API surface is nine endpoints.

Scope and safety: the server can start an SDR capture, so it binds to
loopback unless a host is given explicitly, and supports a shared token for
use on an open network. It is a single-operator local control surface, not
an internet-facing service.
"""

from __future__ import annotations

import json
import mimetypes
import secrets
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from dmr_iq_surveyor import __version__
from dmr_iq_surveyor.web.jobs import Job
from dmr_iq_surveyor.web.service import FieldService, FieldSettings

STATIC_ROOT = Path(__file__).resolve().parent / "static"

# How long an idle server-sent-events reader waits before emitting a
# comment heartbeat. Without it a dropped phone connection is never
# noticed and its handler thread lives until the process exits.
_SSE_HEARTBEAT_SECONDS = 15.0
_MAX_BODY_BYTES = 1_000_000


class _Handler(BaseHTTPRequestHandler):
    server_version = f"dmr-iq-surveyor/{__version__}"
    protocol_version = "HTTP/1.1"

    # -- plumbing ----------------------------------------------------------

    @property
    def service(self) -> FieldService:
        return self.server.service  # type: ignore[attr-defined]

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - base class name
        if getattr(self.server, "verbose", False):  # type: ignore[attr-defined]
            super().log_message(format, *args)

    def _send_json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_error_json(self, status: int, message: str) -> None:
        self._send_json({"error": message}, status=status)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        if length > _MAX_BODY_BYTES:
            raise ValueError("request body is too large")
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid JSON body: {exc}") from exc

    def _authorised(self, query: dict[str, list[str]]) -> bool:
        token = self.service.settings.token
        if not token:
            return True
        supplied = self.headers.get("X-Auth-Token") or (query.get("token") or [""])[0]
        return secrets.compare_digest(supplied, token)

    # -- routing -----------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        path = parsed.path
        # Static assets are served without a token: the page has to load
        # before it can read the token out of its own URL and send it on
        # every API call. Nothing under /static is sensitive.
        if not path.startswith("/api/"):
            self._serve_static("/index.html" if path == "/" else path)
            return
        if not self._authorised(query):
            self._send_error_json(401, "missing or invalid token")
            return
        try:
            if path == "/api/state":
                self._send_json(self.service.state())
            elif path == "/api/position":
                self._send_json(self.service.get_position())
            elif path == "/api/sites":
                self._send_json({"sites": self.service.state()["sites"]})
            elif path == "/api/geojson":
                self._send_json(self.service.geojson())
            elif path == "/api/jobs":
                self._send_json({"jobs": self.service.jobs.list()})
            elif path.startswith("/api/jobs/") and path.endswith("/events"):
                self._stream_events(path.split("/")[3], query)
            elif path.startswith("/api/jobs/"):
                self._send_job(path.split("/")[3])
            else:
                self._send_error_json(404, f"no such endpoint: {path}")
        except BrokenPipeError:
            pass
        except Exception as exc:  # noqa: BLE001 - one bad request must not stop the server
            self._send_error_json(500, f"{type(exc).__name__}: {exc}")

    def do_POST(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        if not self._authorised(query):
            self._send_error_json(401, "missing or invalid token")
            return
        path = parsed.path
        try:
            payload = self._read_json()
            if path == "/api/position":
                self._send_json(self.service.set_position(payload))
            elif path == "/api/capture":
                self._start(lambda: self.service.start_capture(payload))
            elif path == "/api/analyse":
                self._start(lambda: self.service.start_analysis(payload))
            elif path == "/api/solve":
                self._start(lambda: self.service.start_solve(payload))
            elif path.startswith("/api/jobs/") and path.endswith("/cancel"):
                job = self.service.jobs.get(path.split("/")[3])
                if job is None:
                    self._send_error_json(404, "no such job")
                    return
                job.request_cancel()
                self._send_json(job.snapshot())
            else:
                self._send_error_json(404, f"no such endpoint: {path}")
        except ValueError as exc:
            self._send_error_json(400, str(exc))
        except BrokenPipeError:
            pass
        except Exception as exc:  # noqa: BLE001 - one bad request must not stop the server
            self._send_error_json(500, f"{type(exc).__name__}: {exc}")

    # -- handlers ----------------------------------------------------------

    def _start(self, factory: Callable[[], Job]) -> None:
        try:
            job = factory()
        except ValueError as exc:
            self._send_error_json(400, str(exc))
        except RuntimeError as exc:
            # Device unavailable, or another job already running: a
            # precondition the operator can act on, not a server fault.
            self._send_error_json(409, str(exc))
        else:
            self._send_json(job.snapshot(), status=202)

    def _send_job(self, job_id: str) -> None:
        job = self.service.jobs.get(job_id)
        if job is None:
            self._send_error_json(404, "no such job")
            return
        self._send_json(job.snapshot())

    def _stream_events(self, job_id: str, query: dict[str, list[str]]) -> None:
        job = self.service.jobs.get(job_id)
        if job is None:
            self._send_error_json(404, "no such job")
            return
        cursor = int((query.get("cursor") or ["0"])[0])
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        try:
            while True:
                events = job.wait_for_events(cursor, _SSE_HEARTBEAT_SECONDS)
                for event in events:
                    cursor += 1
                    self.wfile.write(
                        f"data: {json.dumps(event)}\n\n".encode()
                    )
                if not events:
                    self.wfile.write(b": heartbeat\n\n")
                self.wfile.flush()
                if job.is_terminal() and cursor >= job.snapshot()["event_count"]:
                    self.wfile.write(
                        f"data: {json.dumps({'stage': 'closed', 'status': job.status})}\n\n".encode()
                    )
                    self.wfile.flush()
                    return
        except (BrokenPipeError, ConnectionResetError):
            return

    def _serve_static(self, path: str) -> None:
        relative = path.lstrip("/") or "index.html"
        candidate = (STATIC_ROOT / relative).resolve()
        # Reject anything that escapes the static root, however it was
        # spelled: this server is reachable from a phone on a shared
        # network and must not serve arbitrary files from the Pi.
        if not candidate.is_relative_to(STATIC_ROOT) or not candidate.is_file():
            self._send_error_json(404, f"not found: {path}")
            return
        body = candidate.read_bytes()
        content_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)


class FieldServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], service: FieldService, *, verbose: bool = False):
        super().__init__(address, _Handler)
        self.service = service
        self.verbose = verbose


def create_server(
    settings: FieldSettings, *, host: str = "127.0.0.1", port: int = 8765, verbose: bool = False
) -> FieldServer:
    Path(settings.output_root).expanduser().resolve().mkdir(parents=True, exist_ok=True)
    Path(settings.recordings_dir).expanduser().resolve().mkdir(parents=True, exist_ok=True)
    return FieldServer((host, port), FieldService(settings), verbose=verbose)


def serve_forever(
    settings: FieldSettings, *, host: str = "127.0.0.1", port: int = 8765, verbose: bool = False
) -> None:
    server = create_server(settings, host=host, port=port, verbose=verbose)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        thread.join()
    except KeyboardInterrupt:
        server.shutdown()
        server.server_close()


__all__ = ["STATIC_ROOT", "FieldServer", "create_server", "serve_forever"]
