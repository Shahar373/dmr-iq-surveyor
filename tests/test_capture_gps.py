from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from dmr_iq_surveyor.capture.gps import GpsFixError, fetch_gps_fix


class _FixedResponseHandler(BaseHTTPRequestHandler):
    """Serves whatever `body`/`status` the test module-level variables hold,
    so each test can swap behavior without spinning a new server."""

    def do_GET(self) -> None:  # noqa: N802
        status = _server_state["status"]
        body = _server_state["body"]
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        pass


_server_state: dict[str, object] = {"status": 200, "body": b"{}"}


@pytest.fixture
def gps_server() -> Iterator[str]:
    server = HTTPServer(("127.0.0.1", 0), _FixedResponseHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/location"
    finally:
        server.shutdown()
        thread.join()


def test_fetch_gps_fix_parses_valid_response(gps_server: str) -> None:
    _server_state["status"] = 200
    _server_state["body"] = json.dumps(
        {"latitude": 32.0853, "longitude": 34.7818, "altitude": 12.5, "accuracy": 8.0}
    ).encode("utf-8")

    fix = fetch_gps_fix(gps_server, timeout_seconds=2.0)

    assert fix.latitude == 32.0853
    assert fix.longitude == 34.7818
    assert fix.altitude_m == 12.5
    assert fix.accuracy_m == 8.0
    assert fix.source_url == gps_server
    assert fix.fetched_at_utc


def test_fetch_gps_fix_accepts_missing_optional_fields(gps_server: str) -> None:
    _server_state["status"] = 200
    _server_state["body"] = json.dumps({"latitude": 1.0, "longitude": 2.0}).encode("utf-8")

    fix = fetch_gps_fix(gps_server, timeout_seconds=2.0)

    assert fix.latitude == 1.0
    assert fix.longitude == 2.0
    assert fix.altitude_m is None
    assert fix.accuracy_m is None


def test_fetch_gps_fix_raises_on_malformed_json(gps_server: str) -> None:
    _server_state["status"] = 200
    _server_state["body"] = b"not json"

    with pytest.raises(GpsFixError, match="invalid JSON"):
        fetch_gps_fix(gps_server, timeout_seconds=2.0)


def test_fetch_gps_fix_raises_on_missing_coordinates(gps_server: str) -> None:
    _server_state["status"] = 200
    _server_state["body"] = json.dumps({"error": "location disabled"}).encode("utf-8")

    with pytest.raises(GpsFixError, match="missing/invalid"):
        fetch_gps_fix(gps_server, timeout_seconds=2.0)


def test_fetch_gps_fix_raises_when_server_unreachable() -> None:
    with pytest.raises(GpsFixError, match="could not reach"):
        fetch_gps_fix("http://127.0.0.1:1/location", timeout_seconds=1.0)
