#!/usr/bin/env python3
"""Tiny GPS-over-HTTP server for Termux on Android.

Runs on the phone that the Raspberry Pi is tethered to. Serves a fresh GPS
fix as JSON on every request, so `dmr-surveyor survey capture --gps-url ...`
on the Pi can fetch real coordinates at capture time with no manual entry.
Requires no dependency beyond the Python standard library and the Termux:API
app (for the `termux-location` command); stdlib-only by design so `pkg
install python` is the only setup step besides Termux:API itself.

Setup (once, in Termux):
    pkg install python termux-api
    # install the "Termux:API" app from F-Droid too -- the pkg above is only
    # the client; the separate app provides the actual Android permission.

Run before each capture session (this process must be running for the
Pi's --gps-url fetch to succeed; stop it with Ctrl+C when done):
    python phone_gps_server.py

Then on the Pi, point --gps-url at this phone's hotspot IP, e.g.:
    --gps-url http://192.168.43.1:8765/location

Each GET /location call runs `termux-location` fresh -- there is no
caching -- so leaving this running for an entire field session and
restarting it before every single capture behave identically.
"""

from __future__ import annotations

import json
import subprocess
import sys
from http.server import BaseHTTPRequestHandler
from socketserver import TCPServer

PORT = 8765
LOCATION_PROVIDER = "network"  # "network" is fast; "gps" is more accurate but slower/needs sky view
LOCATION_TIMEOUT_SECONDS = 20


class GpsRequestHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path != "/location":
            self._respond(404, {"error": f"unknown path {self.path!r}; use /location"})
            return
        try:
            raw = subprocess.check_output(
                ["termux-location", "-p", LOCATION_PROVIDER, "-r", "once"],
                timeout=LOCATION_TIMEOUT_SECONDS,
            )
            fix = json.loads(raw)
        except subprocess.CalledProcessError as exc:
            self._respond(502, {"error": f"termux-location failed: {exc}"})
            return
        except subprocess.TimeoutExpired:
            self._respond(504, {"error": "termux-location timed out; is location enabled?"})
            return
        except json.JSONDecodeError as exc:
            self._respond(502, {"error": f"termux-location returned invalid JSON: {exc}"})
            return
        self._respond(200, fix)

    def _respond(self, status: int, body: dict) -> None:
        payload = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format_str: str, *args: object) -> None:
        sys.stderr.write(f"{self.address_string()} - {format_str % args}\n")


def main() -> None:
    # 0.0.0.0, not localhost: the Pi reaches this over the hotspot interface.
    with TCPServer(("0.0.0.0", PORT), GpsRequestHandler) as httpd:
        print(f"GPS server listening on :{PORT} -- GET /location for a fresh fix")
        print("Press Ctrl+C to stop.")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
