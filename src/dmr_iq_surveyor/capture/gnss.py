"""Read a USB GNSS receiver (NMEA 0183) attached to the Pi itself.

The drive mode was built around the phone: the browser's geolocation watch
posts a fix to `/api/live/position` about once a second. That works, but it
ties every drive to a secure context (HTTPS), to a phone screen that must stay
awake, and to a Wi-Fi hop between the phone and the Pi. A u-blox 7 USB puck
such as the VK-162 plugged into the Pi removes all three: it enumerates as a
CDC-ACM serial port (`/dev/ttyACM0`, or a stable `/dev/serial/by-id/...` link)
and emits plain NMEA sentences at 1 Hz with no configuration.

This module only *reads* positions. It does not decide where they go: the
field service hands each accepted fix to the same in-memory slot a phone fix
lands in, timestamped on this machine's monotonic clock, so the live session's
staleness rule is unchanged.

Dependency-free on purpose -- the field app is a standard-library server so
it cannot fail because a package did not install -- so the port is opened as a
plain file and, when it is a terminal, put into raw mode with `termios`.

What is *not* inferred: NMEA RMC/GGA carry no accuracy in metres. HDOP and
the satellite count are reported as what they are, and `accuracy_m` stays
`None` rather than being invented from HDOP with an assumed range error.
"""

from __future__ import annotations

import glob
import os
import select
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

GNSS_SOURCE_USB = "usb_gnss"
AUTO_DEVICE = "auto"

# Where a USB GNSS puck shows up on Linux. The by-id links are stable across
# reboots and replugs; ttyACM*/ttyUSB* are the fallbacks for a kernel or image
# that does not create them.
_AUTO_PATTERNS = (
    "/dev/serial/by-id/*u-blox*",
    "/dev/serial/by-id/*GPS*",
    "/dev/serial/by-id/*GNSS*",
    "/dev/ttyACM*",
    "/dev/ttyUSB*",
)

STATE_STOPPED = "stopped"
STATE_SEARCHING = "searching"  # no port found / not opened yet
STATE_NO_FIX = "no_fix"  # sentences arriving, receiver has no position yet
STATE_FIX = "fix"
STATE_ERROR = "error"

_KNOTS_TO_MPS = 0.514444


class NmeaError(ValueError):
    """A sentence that is malformed or fails its checksum."""


def _checksum_ok(body: str, given: str) -> bool:
    value = 0
    for character in body:
        value ^= ord(character)
    try:
        return value == int(given, 16)
    except ValueError:
        return False


def _coordinate(value: str, hemisphere: str, degree_digits: int) -> float | None:
    """`ddmm.mmmm` / `dddmm.mmmm` plus N/S/E/W to signed decimal degrees."""
    if not value or not hemisphere:
        return None
    try:
        degrees = float(value[:degree_digits])
        minutes = float(value[degree_digits:])
    except ValueError as exc:
        raise NmeaError(f"bad coordinate {value!r}") from exc
    result = degrees + minutes / 60.0
    if hemisphere in ("S", "W"):
        result = -result
    elif hemisphere not in ("N", "E"):
        raise NmeaError(f"bad hemisphere {hemisphere!r}")
    return result


def _float_or_none(value: str) -> float | None:
    if value == "":
        return None
    try:
        return float(value)
    except ValueError as exc:
        raise NmeaError(f"bad number {value!r}") from exc


def parse_nmea(line: str) -> dict[str, Any] | None:
    """Parse one NMEA sentence.

    Returns a dict for RMC and GGA from any talker (GP, GN, GL, GA, BD),
    `None` for any other sentence type, and raises `NmeaError` for a sentence
    that is malformed or whose checksum does not match. A sentence without a
    checksum is refused: on a serial line a corrupted digit in a latitude is
    otherwise indistinguishable from a real position.
    """
    text = line.strip()
    if not text.startswith("$"):
        return None
    if "*" not in text:
        raise NmeaError("sentence has no checksum")
    body, _, given = text[1:].partition("*")
    if not _checksum_ok(body, given[:2]):
        raise NmeaError("checksum mismatch")
    fields = body.split(",")
    tag = fields[0]
    if len(tag) != 5:
        return None
    kind = tag[2:]
    if kind == "RMC":
        if len(fields) < 10:
            raise NmeaError("short RMC sentence")
        speed_knots = _float_or_none(fields[7])
        return {
            "type": "RMC",
            "talker": tag[:2],
            "time_utc": fields[1],
            "valid": fields[2] == "A",
            "latitude": _coordinate(fields[3], fields[4], 2),
            "longitude": _coordinate(fields[5], fields[6], 3),
            "speed_mps": speed_knots * _KNOTS_TO_MPS if speed_knots is not None else None,
            "course_deg": _float_or_none(fields[8]),
            "date": fields[9],
        }
    if kind == "GGA":
        if len(fields) < 10:
            raise NmeaError("short GGA sentence")
        quality = int(fields[6]) if fields[6].isdigit() else 0
        satellites = int(fields[7]) if fields[7].isdigit() else None
        return {
            "type": "GGA",
            "talker": tag[:2],
            "time_utc": fields[1],
            "latitude": _coordinate(fields[2], fields[3], 2),
            "longitude": _coordinate(fields[4], fields[5], 3),
            "quality": quality,
            "satellites": satellites,
            "hdop": _float_or_none(fields[8]),
            "altitude_m": _float_or_none(fields[9]),
        }
    return None


@dataclass(slots=True)
class GnssFix:
    latitude: float
    longitude: float
    speed_mps: float | None
    course_deg: float | None
    satellites: int | None
    hdop: float | None
    altitude_m: float | None
    time_utc: str
    # Not reported by RMC/GGA. Left absent rather than derived from HDOP.
    accuracy_m: float | None = None
    source: str = GNSS_SOURCE_USB

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class FixAssembler:
    """Combine RMC (position, validity, speed) with GGA (quality, HDOP).

    One fix is emitted per valid RMC. GGA is used only when it describes the
    same epoch (same UTC time field); a GGA saying "no fix" (quality 0) for
    that epoch vetoes the RMC, because the two disagreeing means one of them
    is wrong and a misplaced measurement is the error this system cannot see
    afterwards.
    """

    def __init__(self) -> None:
        self._gga: dict[str, Any] | None = None

    def feed(self, sentence: dict[str, Any]) -> GnssFix | None:
        if sentence["type"] == "GGA":
            self._gga = sentence
            return None
        if sentence["type"] != "RMC" or not sentence["valid"]:
            return None
        if sentence["latitude"] is None or sentence["longitude"] is None:
            return None
        gga = self._gga if self._gga and self._gga["time_utc"] == sentence["time_utc"] else None
        if gga is not None and gga["quality"] == 0:
            return None
        return GnssFix(
            latitude=float(sentence["latitude"]),
            longitude=float(sentence["longitude"]),
            speed_mps=sentence["speed_mps"],
            course_deg=sentence["course_deg"],
            satellites=gga["satellites"] if gga else None,
            hdop=gga["hdop"] if gga else None,
            altitude_m=gga["altitude_m"] if gga else None,
            time_utc=sentence["time_utc"],
        )

    def last_gga(self) -> dict[str, Any] | None:
        return self._gga


def resolve_device(path: str) -> str | None:
    """`auto` to the first GNSS-looking serial port present, else `path`."""
    if path != AUTO_DEVICE:
        return path
    for pattern in _AUTO_PATTERNS:
        matches = sorted(glob.glob(pattern))
        if matches:
            return matches[0]
    return None


class LineSource(Protocol):
    """An open NMEA stream: yields lines, `None` on a read timeout."""

    def lines(self) -> Iterator[str | None]: ...

    def close(self) -> None: ...


class SerialLineSource:
    """A serial port opened as a file. Raw mode, 9600 baud when it is a tty.

    A CDC-ACM device ignores the baud rate, but a USB-serial bridge
    (`ttyUSB*`) does not, and 9600 is the u-blox default.
    """

    def __init__(self, path: str, *, read_timeout_s: float = 1.0) -> None:
        self.path = path
        self._timeout = read_timeout_s
        self._fd = os.open(path, os.O_RDONLY | os.O_NOCTTY | os.O_NONBLOCK)
        try:
            self._configure()
        except OSError:
            pass  # not a terminal (a FIFO in a test, a pty): read it as-is

    def _configure(self) -> None:
        import termios
        import tty

        tty.setraw(self._fd)
        attributes = termios.tcgetattr(self._fd)
        attributes[4] = termios.B9600  # ispeed
        attributes[5] = termios.B9600  # ospeed
        termios.tcsetattr(self._fd, termios.TCSANOW, attributes)

    def lines(self) -> Iterator[str | None]:
        pending = b""
        while True:
            ready, _, _ = select.select([self._fd], [], [], self._timeout)
            if not ready:
                yield None
                continue
            chunk = os.read(self._fd, 4096)
            if not chunk:
                raise OSError(f"{self.path}: end of stream (device unplugged?)")
            pending += chunk
            while b"\n" in pending:
                raw, pending = pending.split(b"\n", 1)
                yield raw.decode("ascii", errors="replace")
            if len(pending) > 8192:
                pending = b""  # no newline in 8 KiB: not NMEA, start over

    def close(self) -> None:
        try:
            os.close(self._fd)
        except OSError:
            pass


@dataclass(slots=True)
class GnssStatus:
    configured: str
    device: str | None = None
    state: str = STATE_SEARCHING
    error: str = ""
    sentences: int = 0
    bad_sentences: int = 0
    fixes: int = 0
    satellites: int | None = None
    hdop: float | None = None
    last_fix: dict[str, Any] | None = None
    last_sentence_at: float | None = None
    last_fix_at: float | None = None
    opened_at_utc: str | None = None
    reconnects: int = 0
    extra: dict[str, Any] = field(default_factory=dict)


class GnssReader:
    """Background thread: open the port, parse, hand each fix to `on_fix`.

    Never raises into the caller. A missing or unplugged device becomes a
    state (`searching` / `error`) with the reason, and the port is retried
    every `retry_s`, because a puck knocked loose in a car should come back by
    itself once it is plugged in again rather than need the app restarted.
    """

    def __init__(
        self,
        device: str,
        on_fix: Callable[[GnssFix], None],
        *,
        opener: Callable[[str], LineSource] | None = None,
        clock: Callable[[], float] = time.monotonic,
        retry_s: float = 2.0,
    ) -> None:
        self._configured = device
        self._on_fix = on_fix
        self._opener = opener or SerialLineSource
        self._clock = clock
        self._retry_s = retry_s
        self._lock = threading.Lock()
        self._status = GnssStatus(configured=device)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._source: LineSource | None = None

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="usb-gnss", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 3.0) -> None:
        self._stop.set()
        source = self._source
        if source is not None:
            source.close()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        with self._lock:
            self._status.state = STATE_STOPPED

    # -- status ------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        now = self._clock()
        with self._lock:
            status = asdict(self._status)
        sentence_at = status.pop("last_sentence_at")
        fix_at = status.pop("last_fix_at")
        status["last_sentence_age_s"] = None if sentence_at is None else round(now - sentence_at, 1)
        status["fix_age_s"] = None if fix_at is None else round(now - fix_at, 1)
        status.pop("extra", None)
        return status

    # -- the loop ----------------------------------------------------------

    def _set(self, **fields: Any) -> None:
        with self._lock:
            for key, value in fields.items():
                setattr(self._status, key, value)

    def _run(self) -> None:
        first = True
        while not self._stop.is_set():
            path = resolve_device(self._configured)
            if path is None:
                self._set(state=STATE_SEARCHING, device=None,
                          error="no GNSS serial port found (is the receiver plugged in?)")
                self._stop.wait(self._retry_s)
                continue
            try:
                self._source = self._opener(path)
            except OSError as exc:
                self._set(state=STATE_ERROR, device=path, error=f"cannot open {path}: {exc}")
                self._stop.wait(self._retry_s)
                continue
            with self._lock:
                self._status.device = path
                self._status.state = STATE_NO_FIX
                self._status.error = ""
                self._status.opened_at_utc = datetime.now(UTC).isoformat()
                if not first:
                    self._status.reconnects += 1
            first = False
            try:
                self._consume(self._source)
            except OSError as exc:
                self._set(state=STATE_ERROR, error=f"{path}: {exc}")
            finally:
                self._source.close()
                self._source = None
            if not self._stop.is_set():
                self._stop.wait(self._retry_s)

    def _consume(self, source: LineSource) -> None:
        assembler = FixAssembler()
        for line in source.lines():
            if self._stop.is_set():
                return
            if line is None:
                continue
            try:
                sentence = parse_nmea(line)
            except NmeaError:
                with self._lock:
                    self._status.bad_sentences += 1
                continue
            now = self._clock()
            with self._lock:
                self._status.last_sentence_at = now
                self._status.sentences += 1
            if sentence is None:
                continue
            fix = assembler.feed(sentence)
            if sentence["type"] == "GGA":
                with self._lock:
                    self._status.satellites = sentence["satellites"]
                    self._status.hdop = sentence["hdop"]
                    if sentence["quality"] == 0:
                        self._status.state = STATE_NO_FIX
            elif sentence["type"] == "RMC" and not sentence["valid"]:
                self._set(state=STATE_NO_FIX)
            if fix is None:
                continue
            with self._lock:
                self._status.state = STATE_FIX
                self._status.fixes += 1
                self._status.last_fix_at = now
                self._status.last_fix = fix.to_dict()
            try:
                self._on_fix(fix)
            except Exception as exc:  # noqa: BLE001 - a consumer bug must not kill the reader
                self._set(error=f"fix consumer failed: {type(exc).__name__}: {exc}")


__all__ = [
    "AUTO_DEVICE",
    "FixAssembler",
    "GNSS_SOURCE_USB",
    "GnssFix",
    "GnssReader",
    "NmeaError",
    "SerialLineSource",
    "parse_nmea",
    "resolve_device",
]
