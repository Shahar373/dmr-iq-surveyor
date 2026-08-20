#!/data/data/com.termux/files/usr/bin/bash
# One-time setup, run inside Termux on the phone, for the GPS-over-HTTP
# server used by `dmr-surveyor survey capture --gps-url ...` on the Pi.
#
# Prerequisites (manual, one-time, cannot be automated from a script):
#   1. Install the "Termux" app -- from F-Droid, not the Play Store (the
#      Play Store build is outdated and can't install packages reliably).
#   2. Install the separate "Termux:API" app, same source.
#   3. Open Termux and run: termux-setup-storage
#      (grants Termux access to your Downloads folder; tap "Allow" on the
#      permission prompt).
#
# Then, with this file in your Downloads folder, run in Termux:
#   bash storage/downloads/dmr_gps_setup.sh
#
# This writes phone_gps_server.py to your Termux home directory and (if you
# later install the "Termux:Widget" app and add a widget to your home
# screen) a one-tap "GPS Server" launcher -- no typing needed in the field
# after this one-time setup.

set -euo pipefail

if [ -z "${PREFIX:-}" ] || [ "${PREFIX##*/}" != "usr" ]; then
    echo "This script must be run inside Termux, not a regular Linux shell." >&2
    exit 1
fi

echo "==> Installing python and termux-api..."
pkg install -y python termux-api

SERVER_PATH="$HOME/phone_gps_server.py"
echo "==> Writing GPS server to $SERVER_PATH"
cat > "$SERVER_PATH" <<'PYEOF'
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
PYEOF
chmod +x "$SERVER_PATH"

mkdir -p "$HOME/.shortcuts"
cat > "$HOME/.shortcuts/GPS Server.sh" <<'SHEOF'
#!/data/data/com.termux/files/usr/bin/bash
cd "$HOME"
python phone_gps_server.py
SHEOF
chmod +x "$HOME/.shortcuts/GPS Server.sh"

echo ""
echo "==> Setup complete."
echo ""
echo "If you install the separate 'Termux:Widget' app (F-Droid) and add a"
echo "Termux widget to your home screen, a 'GPS Server' icon will appear --"
echo "one tap starts the server, no typing needed in the field."
echo ""
echo "To start it manually instead, every time before a capture:"
echo "    python \$HOME/phone_gps_server.py"
echo ""
echo "Your phone's IP addresses (use the one on your hotspot/Wi-Fi interface"
echo "as --gps-url on the Pi, e.g. http://<this-ip>:8765/location):"
ip -4 addr show 2>/dev/null | grep -oE 'inet [0-9]+(\.[0-9]+){3}' | awk '{print $2}' | grep -v '^127\.' || true
