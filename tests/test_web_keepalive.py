"""Several requests on one HTTP/1.1 keep-alive connection each get an answer.

The handler's "a response has already started" guard used to be reset once
per connection rather than once per request. Every request after the first on
a reused socket was then answered by closing it, and a browser resends such a
request on a fresh connection -- so a POST ran twice. This drives the real
server over one `http.client` connection, the way a browser reuses sockets.
"""

from __future__ import annotations

import http.client
import json
import threading
from pathlib import Path

from fixtures.device_probe import StubProbeRunner, present
from fixtures.geo_scenario import build_database

from dmr_iq_surveyor.web.server import create_server
from dmr_iq_surveyor.web.service import FieldSettings


def test_every_request_on_a_kept_alive_connection_is_answered_once(tmp_path: Path) -> None:
    database = tmp_path / "db.sqlite3"
    build_database(database).close()
    settings = FieldSettings(
        database_path=database,
        output_root=tmp_path / "out",
        recordings_dir=tmp_path / "rec",
    )
    server = create_server(
        settings, host="127.0.0.1", port=0, probe_runner=StubProbeRunner(present("fake SDR"))
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    stored: list[dict] = []
    original = server.service.push_live_position

    def counting(payload: dict) -> dict:
        stored.append(payload)
        return original(payload)

    server.service.push_live_position = counting  # type: ignore[method-assign]
    connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
    try:
        for index in range(3):
            body = json.dumps({"latitude": 32.0 + index / 1000, "longitude": 34.8})
            connection.request(
                "POST", "/api/live/position", body=body,
                headers={"Content-Type": "application/json"},
            )
            response = connection.getresponse()
            assert response.status == 200, f"request {index} on the same socket got no answer"
            assert json.loads(response.read())["accepted"] is True
        connection.request("GET", "/api/live")
        response = connection.getresponse()
        assert response.status == 200
        response.read()
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        server.service.close()
    assert len(stored) == 3, "each POST must be handled exactly once"
