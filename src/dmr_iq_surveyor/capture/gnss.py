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

**Sharing the receiver with gpsd.** A serial port has one reader. On a Pi
where gpsd already owns the puck -- because another application on the same
machine uses it, or because chrony takes the time from it -- opening the port
here fails with EBUSY, and *succeeding* would be worse: two readers splitting
one byte stream each see corrupted sentences, and gpsd's clients (the clock
among them) lose their source. So the reader is also a plain gpsd JSON client
(`gpsd` or `gpsd://host:port`), and `auto` uses gpsd whenever one is listening
on the standard port, opening the serial port only when none is. In gpsd mode
`accuracy_m` is gpsd's own horizontal error estimate (`eph`, or the larger of
`epx`/`epy`), reported as gpsd's estimate.
"""

from __future__ import annotations

import glob
import json
import os
import select
import socket
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

GNSS_SOURCE_USB = "usb_gnss"
AUTO_DEVICE = "auto"
GPSD_SCHEME = "gpsd"
GPSD_DEFAULT_HOST = "127.0.0.1"
GPSD_DEFAULT_PORT = 2947
_GPSD_WATCH = b'?WATCH={"enable":true,"json":true};\n'

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

    Receivers disagree on the order within an epoch. A u-blox 7 sends RMC
    first (RMC, VTG, GGA, GSA, GSV, GLL); others send GGA first. So a valid
    RMC whose GGA has already arrived is emitted at once, and one whose GGA
    has not is *held*: it is emitted, with GGA's satellites, HDOP and
    altitude, when the same-epoch GGA arrives (or suppressed, if that GGA
    says quality 0). If the epoch ends without one -- the next RMC or a GGA
    for another time arrives, or the reader calls `flush()` -- the held RMC
    is emitted on its own, as it would have been with no GGA at all.
    """

    def __init__(self) -> None:
        self._gga: dict[str, Any] | None = None
        # A held RMC, with its same-epoch GGA if one was already in hand.
        self._pending: tuple[dict[str, Any], dict[str, Any] | None] | None = None

    def feed(self, sentence: dict[str, Any]) -> GnssFix | None:
        if sentence["type"] == "GGA":
            self._gga = sentence
            if self._pending is None:
                return None
            rmc, matched = self._pending
            self._pending = None
            if rmc["time_utc"] == sentence["time_utc"]:
                return self._build(rmc, sentence)
            return self._build(rmc, matched)  # its epoch ended without a new GGA
        if sentence["type"] != "RMC":
            return None
        # An RMC for another time starts a new epoch: the held one's GGA never came.
        flushed = None
        if self._pending is not None and self._pending[0]["time_utc"] != sentence["time_utc"]:
            flushed = self.flush()
        if not sentence["valid"]:
            return flushed
        if sentence["latitude"] is None or sentence["longitude"] is None:
            return flushed
        gga = self._gga if self._gga and self._gga["time_utc"] == sentence["time_utc"] else None
        if gga is None or flushed is not None:
            # Wait for this epoch's GGA. (When an older RMC was just flushed
            # this call has already returned a fix; this one waits its turn.)
            self._pending = (sentence, gga)
            return flushed
        return self._build(sentence, gga)

    def flush(self) -> GnssFix | None:
        """Emit a held RMC whose epoch produced no GGA; None if none is held."""
        pending, self._pending = self._pending, None
        return self._build(*pending) if pending is not None else None

    @staticmethod
    def _build(rmc: dict[str, Any], gga: dict[str, Any] | None) -> GnssFix | None:
        if gga is not None and gga["quality"] == 0:
            return None
        return GnssFix(
            latitude=float(rmc["latitude"]),
            longitude=float(rmc["longitude"]),
            speed_mps=rmc["speed_mps"],
            course_deg=rmc["course_deg"],
            satellites=gga["satellites"] if gga else None,
            hdop=gga["hdop"] if gga else None,
            altitude_m=gga["altitude_m"] if gga else None,
            time_utc=rmc["time_utc"],
        )

    def last_gga(self) -> dict[str, Any] | None:
        return self._gga


def parse_gpsd_address(device: str) -> tuple[str, int] | None:
    """`gpsd` / `gpsd://host` / `gpsd://host:port` to (host, port); else None."""
    if device == GPSD_SCHEME:
        return GPSD_DEFAULT_HOST, GPSD_DEFAULT_PORT
    prefix = GPSD_SCHEME + "://"
    if not device.startswith(prefix):
        return None
    rest = device[len(prefix):].strip("/")
    host, _, port = rest.partition(":")
    try:
        return (host or GPSD_DEFAULT_HOST, int(port) if port else GPSD_DEFAULT_PORT)
    except ValueError as exc:
        raise ValueError(f"bad gpsd address {device!r}") from exc


def gpsd_listening(
    host: str = GPSD_DEFAULT_HOST, port: int = GPSD_DEFAULT_PORT, timeout: float = 0.5
) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def resolve_device(path: str, *, gpsd_probe: Callable[[], bool] = gpsd_listening) -> str | None:
    """`auto` to gpsd if one is running, else the first GNSS-looking serial
    port present; anything else is returned as given."""
    if path != AUTO_DEVICE:
        return path
    if gpsd_probe():
        # gpsd owns the receiver. Opening the port as well would either fail
        # (EBUSY) or split its byte stream with gpsd -- never both work.
        return f"{GPSD_SCHEME}://{GPSD_DEFAULT_HOST}:{GPSD_DEFAULT_PORT}"
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
        import termios

        try:
            self._configure()
        except (OSError, termios.error):
            # Not a terminal (a FIFO, a socat pipe, a file replay): read it
            # as-is. termios raises its own error, which is not an OSError.
            pass
        except BaseException:
            self.close()  # do not leak the descriptor
            raise

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


class GpsdLineSource:
    """A gpsd JSON stream: connect, ask for JSON reports, yield lines.

    Read timeouts yield `None` rather than ending the stream, because gpsd
    with no device attached sends nothing at all, and that is not a
    disconnection.
    """

    def __init__(self, host: str, port: int, *, read_timeout_s: float = 1.0) -> None:
        self.path = f"{GPSD_SCHEME}://{host}:{port}"
        self._socket = socket.create_connection((host, port), timeout=5.0)
        self._socket.settimeout(read_timeout_s)
        self._socket.sendall(_GPSD_WATCH)

    def lines(self) -> Iterator[str | None]:
        pending = b""
        while True:
            try:
                chunk = self._socket.recv(65536)
            except TimeoutError:
                yield None
                continue
            if not chunk:
                raise OSError(f"{self.path}: gpsd closed the connection")
            pending += chunk
            while b"\n" in pending:
                raw, pending = pending.split(b"\n", 1)
                yield raw.decode("utf-8", errors="replace")
            if len(pending) > 1 << 20:
                raise OSError(f"{self.path}: no line end in 1 MiB; not gpsd")

    def close(self) -> None:
        try:
            self._socket.close()
        except OSError:
            pass


def default_opener(path: str) -> LineSource:
    address = parse_gpsd_address(path)
    if address is not None:
        return GpsdLineSource(*address)
    return SerialLineSource(path)


def _finite(value: Any, low: float, high: float) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if low <= number <= high else None


def parse_gpsd_tpv(message: dict[str, Any]) -> GnssFix | None:
    """A gpsd TPV report to a fix, or None when it carries no usable position.

    Mode 0/1 is no fix. (0, 0) is a receiver that has not locked, not a
    position in the Gulf of Guinea.
    """
    if message.get("class") != "TPV":
        return None
    mode = _finite(message.get("mode"), 0, 3)
    if mode is None or mode < 2:
        return None
    latitude = _finite(message.get("lat"), -90.0, 90.0)
    longitude = _finite(message.get("lon"), -180.0, 180.0)
    if latitude is None or longitude is None or (latitude == 0.0 and longitude == 0.0):
        return None
    accuracy = _finite(message.get("eph"), 0.0, 1e6)
    if accuracy is None:
        parts = [
            value for value in (
                _finite(message.get("epx"), 0.0, 1e6), _finite(message.get("epy"), 0.0, 1e6)
            ) if value is not None
        ]
        accuracy = max(parts) if parts else None
    altitude = _finite(message.get("altMSL", message.get("alt")), -1e4, 1e5)
    return GnssFix(
        latitude=latitude,
        longitude=longitude,
        speed_mps=_finite(message.get("speed"), 0.0, 1000.0),
        course_deg=_finite(message.get("track"), 0.0, 360.0),
        satellites=None,
        hdop=None,
        altitude_m=altitude,
        time_utc=str(message.get("time") or ""),
        accuracy_m=accuracy,
    )


def parse_gpsd_sky(message: dict[str, Any]) -> tuple[int | None, float | None] | None:
    """A gpsd SKY report to (satellites used, HDOP), or None."""
    if message.get("class") != "SKY":
        return None
    used = _finite(message.get("uSat"), 0, 999)
    satellites = message.get("satellites")
    if used is None and isinstance(satellites, list):
        used = sum(1 for sat in satellites if isinstance(sat, dict) and sat.get("used") is True)
    return (int(used) if used is not None else None, _finite(message.get("hdop"), 0.0, 99.99))


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
        self._opener = opener or default_opener
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
                hint = ""
                if getattr(exc, "errno", None) == 16:  # EBUSY
                    hint = (" -- another program (usually gpsd) holds the receiver; use "
                            "--gnss-device gpsd (or auto) to read it through gpsd")
                self._set(state=STATE_ERROR, device=path,
                          error=f"cannot open {path}: {exc}{hint}")
                self._stop.wait(self._retry_s)
                continue
            except Exception as exc:  # noqa: BLE001 - the thread must outlive any open failure
                # A bad gpsd address, a termios failure, a driver oddity: an
                # error state with the reason, retried, never a dead thread
                # that leaves the status at "searching" forever.
                self._set(state=STATE_ERROR, device=path,
                          error=f"cannot open {path}: {type(exc).__name__}: {exc}")
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
                # A quiet read timeout: no GGA is coming for a held RMC.
                held = assembler.flush()
                if held is not None:
                    self._deliver(held, self._clock())
                continue
            if line.lstrip().startswith("{"):
                self._consume_gpsd(line)
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
            if fix is not None:
                self._deliver(fix, now)
            # The current sentence has the last word on the state: a fix the
            # assembler emits can be a held RMC from the previous epoch.
            if sentence["type"] == "GGA" and sentence["quality"] == 0:
                self._set(state=STATE_NO_FIX)
            elif sentence["type"] == "RMC" and not sentence["valid"]:
                self._set(state=STATE_NO_FIX)

    def _deliver(self, fix: GnssFix, now: float) -> None:
        with self._lock:
            self._status.state = STATE_FIX
            self._status.fixes += 1
            self._status.last_fix_at = now
            self._status.last_fix = fix.to_dict()
        try:
            self._on_fix(fix)
        except Exception as exc:  # noqa: BLE001 - a consumer bug must not kill the reader
            self._set(error=f"fix consumer failed: {type(exc).__name__}: {exc}")

    def _consume_gpsd(self, line: str) -> None:
        try:
            message = json.loads(line)
        except ValueError:
            with self._lock:
                self._status.bad_sentences += 1
            return
        if not isinstance(message, dict):
            return
        now = self._clock()
        with self._lock:
            self._status.last_sentence_at = now
            self._status.sentences += 1
        kind = message.get("class")
        if kind == "DEVICES":
            devices = message.get("devices")
            if isinstance(devices, list) and not devices:
                self._set(state=STATE_SEARCHING,
                          error="gpsd is running but has no GNSS device (is it plugged in?)")
            return
        if kind == "SKY":
            parsed = parse_gpsd_sky(message)
            if parsed is not None:
                with self._lock:
                    used, hdop = parsed
                    if used is not None:
                        self._status.satellites = used
                    if hdop is not None:
                        self._status.hdop = hdop
            return
        if kind != "TPV":
            return
        fix = parse_gpsd_tpv(message)
        if fix is None:
            self._set(state=STATE_NO_FIX, error="")
            return
        with self._lock:
            fix.satellites = self._status.satellites
            fix.hdop = self._status.hdop
            self._status.state = STATE_FIX
            self._status.error = ""
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
    "GpsdLineSource",
    "parse_gpsd_address",
    "parse_gpsd_sky",
    "parse_gpsd_tpv",
    "NmeaError",
    "SerialLineSource",
    "parse_nmea",
    "resolve_device",
]
