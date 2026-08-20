"""Fetch a live GPS fix from a phone-hosted HTTP endpoint at capture time.

Field captures happen while moving between sites; the Raspberry Pi has no
GPS of its own but is normally tethered to a phone's hotspot. Rather than
requiring the operator to type coordinates before every recording, this
module fetches a fresh fix from a small HTTP server run on the phone (see
`scripts/phone_gps_server.py`, built for Termux + Termux:API's
`termux-location`) at the moment of capture.

Never required: `capture/core.py::run_capture_and_survey` treats a missing
`gps_url`, an unreachable server, or a malformed response the same way -- the
RF capture always proceeds, and the run is stored with `gps_source` set to
`not_configured` or `fetch_failed` rather than aborting. "Missing is not
null": the reason GPS is absent is always recorded, never silently dropped.
"""

from __future__ import annotations

import json
import urllib.request
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

GPS_SOURCE_PHONE = "phone_gps"


class GpsFixError(Exception):
    """Raised when a GPS fix could not be fetched or parsed."""


@dataclass(slots=True)
class GpsFix:
    latitude: float
    longitude: float
    altitude_m: float | None
    accuracy_m: float | None
    source_url: str
    fetched_at_utc: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def fetch_gps_fix(url: str, *, timeout_seconds: float = 10.0) -> GpsFix:
    """GET `url` and parse a JSON body with `latitude`/`longitude` keys
    (matching Termux's `termux-location` field names) plus optional
    `altitude`/`accuracy`. Raises `GpsFixError` on any failure -- network,
    HTTP status, or malformed payload -- callers decide whether that's
    fatal (it never is, for `run_capture_and_survey`)."""
    try:
        with urllib.request.urlopen(url, timeout=timeout_seconds) as response:
            payload = response.read()
    except OSError as exc:
        raise GpsFixError(f"could not reach GPS server at {url!r}: {exc}") from exc

    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise GpsFixError(f"GPS server at {url!r} returned invalid JSON: {exc}") from exc

    try:
        latitude = float(data["latitude"])
        longitude = float(data["longitude"])
    except (KeyError, TypeError, ValueError) as exc:
        raise GpsFixError(
            f"GPS server at {url!r} response missing/invalid latitude or longitude: {data!r}"
        ) from exc

    altitude = data.get("altitude")
    accuracy = data.get("accuracy")
    return GpsFix(
        latitude=latitude,
        longitude=longitude,
        altitude_m=float(altitude) if altitude is not None else None,
        accuracy_m=float(accuracy) if accuracy is not None else None,
        source_url=url,
        fetched_at_utc=datetime.now(UTC).isoformat(),
    )


def resolve_gps(
    *,
    gps_url: str | None = None,
    gps_timeout_seconds: float = 10.0,
    latitude: float | None = None,
    longitude: float | None = None,
) -> dict[str, Any]:
    """Resolve coordinates for one run, never raising.

    Precedence: an explicit `latitude`/`longitude` pair wins over a live
    `gps_url` fetch. The returned `source` always says exactly why
    coordinates are or aren't present -- `user`, `phone_gps`,
    `fetch_failed` (with `error` set) or `not_configured` -- so a caller can
    record the reason instead of storing a bare NULL.
    """
    resolved: dict[str, Any] = {
        "source": "not_configured",
        "latitude": None,
        "longitude": None,
        "altitude_m": None,
        "accuracy_m": None,
        "fetched_at_utc": None,
        "error": None,
    }
    if latitude is not None and longitude is not None:
        resolved.update(source="user", latitude=latitude, longitude=longitude)
        return resolved
    if not gps_url:
        return resolved
    try:
        fix = fetch_gps_fix(gps_url, timeout_seconds=gps_timeout_seconds)
    except GpsFixError as exc:
        resolved.update(source="fetch_failed", error=str(exc))
        return resolved
    resolved.update(
        source=GPS_SOURCE_PHONE,
        latitude=fix.latitude,
        longitude=fix.longitude,
        altitude_m=fix.altitude_m,
        accuracy_m=fix.accuracy_m,
        fetched_at_utc=fix.fetched_at_utc,
    )
    return resolved


__all__ = ["GPS_SOURCE_PHONE", "GpsFix", "GpsFixError", "fetch_gps_fix", "resolve_gps"]
