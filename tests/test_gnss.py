"""The Pi-side USB GNSS reader: NMEA parsing, fix assembly, the reader loop.

Sentences are the ones a u-blox 7 (VK-162) emits at 1 Hz, with checksums
computed here so a test can never pass on a sentence the parser should have
refused.
"""

from __future__ import annotations

import time
from collections.abc import Iterator

import pytest

from dmr_iq_surveyor.capture.gnss import (
    AUTO_DEVICE,
    FixAssembler,
    GnssFix,
    GnssReader,
    NmeaError,
    parse_nmea,
    resolve_device,
)


def nmea(body: str) -> str:
    checksum = 0
    for character in body:
        checksum ^= ord(character)
    return f"${body}*{checksum:02X}"


RMC = nmea("GPRMC,083559.00,A,3204.1234,N,03448.5678,E,27.0,90.5,101126,,,A")
GGA = nmea("GPGGA,083559.00,3204.1234,N,03448.5678,E,1,09,0.95,42.1,M,18.0,M,,")
RMC_NO_FIX = nmea("GPRMC,083600.00,V,,,,,,,101126,,,N")
GGA_NO_FIX = nmea("GPGGA,083600.00,,,,,0,00,99.99,,,,,,")


def test_rmc_is_parsed_to_signed_decimal_degrees_and_metres_per_second() -> None:
    parsed = parse_nmea(RMC)
    assert parsed["type"] == "RMC" and parsed["valid"]
    assert parsed["latitude"] == pytest.approx(32 + 4.1234 / 60)
    assert parsed["longitude"] == pytest.approx(34 + 48.5678 / 60)
    assert parsed["speed_mps"] == pytest.approx(27.0 * 0.514444)


def test_southern_and_western_hemispheres_are_negative() -> None:
    parsed = parse_nmea(nmea("GNRMC,000000.00,A,3300.0000,S,07000.0000,W,0.0,,010126,,,A"))
    assert parsed["latitude"] == pytest.approx(-33.0)
    assert parsed["longitude"] == pytest.approx(-70.0)


def test_gga_carries_quality_satellites_and_hdop() -> None:
    parsed = parse_nmea(GGA)
    assert parsed["quality"] == 1 and parsed["satellites"] == 9 and parsed["hdop"] == 0.95


def test_a_corrupted_sentence_is_refused_not_believed() -> None:
    corrupted = RMC.replace("3204.1234", "3214.1234")
    with pytest.raises(NmeaError):
        parse_nmea(corrupted)
    with pytest.raises(NmeaError):
        parse_nmea(RMC.split("*")[0])  # no checksum at all


def test_other_sentences_are_ignored() -> None:
    assert parse_nmea(nmea("GPGSV,3,1,11,01,40,083,46")) is None
    assert parse_nmea("garbage") is None


def test_assembler_emits_one_fix_per_valid_rmc_with_matching_gga() -> None:
    assembler = FixAssembler()
    assert assembler.feed(parse_nmea(GGA)) is None
    fix = assembler.feed(parse_nmea(RMC))
    assert isinstance(fix, GnssFix)
    assert fix.satellites == 9 and fix.hdop == 0.95 and fix.altitude_m == 42.1
    # Accuracy in metres is not in NMEA RMC/GGA and is not invented.
    assert fix.accuracy_m is None


def test_no_fix_produces_no_position() -> None:
    assembler = FixAssembler()
    assembler.feed(parse_nmea(GGA_NO_FIX))
    assert assembler.feed(parse_nmea(RMC_NO_FIX)) is None


def test_gga_saying_no_fix_for_the_same_epoch_vetoes_the_rmc() -> None:
    assembler = FixAssembler()
    assembler.feed(parse_nmea(nmea("GPGGA,083559.00,,,,,0,00,99.99,,,,,,")))
    assert assembler.feed(parse_nmea(RMC)) is None


def test_resolve_device_passes_an_explicit_path_through() -> None:
    assert resolve_device("/dev/ttyACM7") == "/dev/ttyACM7"
    # 'auto' looks for ports; in a container there may be none, which is an
    # answer (None), never an exception.
    resolved = resolve_device(AUTO_DEVICE)
    assert resolved is None or resolved.startswith("/dev/")


class _FakeSource:
    def __init__(self, lines: list[str | None], *, then_fail: bool = False) -> None:
        self._lines = lines
        self._then_fail = then_fail
        self.closed = False

    def lines(self) -> Iterator[str | None]:
        yield from self._lines
        if self._then_fail:
            raise OSError("device unplugged")
        while True:
            time.sleep(0.01)
            yield None

    def close(self) -> None:
        self.closed = True


def _wait(predicate, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition not met in time")


def test_reader_delivers_fixes_and_reports_state() -> None:
    fixes: list[GnssFix] = []
    sources: list[_FakeSource] = []

    def opener(path: str) -> _FakeSource:
        source = _FakeSource([GGA_NO_FIX, RMC_NO_FIX, "noise", GGA, RMC])
        sources.append(source)
        return source

    reader = GnssReader("/dev/fake", fixes.append, opener=opener)
    reader.start()
    try:
        _wait(lambda: fixes)
        status = reader.status()
        assert status["state"] == "fix"
        assert status["device"] == "/dev/fake"
        assert status["satellites"] == 9 and status["fixes"] == 1
        assert status["fix_age_s"] is not None
    finally:
        reader.stop()
    assert reader.status()["state"] == "stopped"
    assert sources[0].closed


def test_unplugged_receiver_is_reported_and_retried() -> None:
    opened: list[int] = []

    def opener(path: str) -> _FakeSource:
        opened.append(1)
        return _FakeSource([GGA, RMC], then_fail=True)

    reader = GnssReader("/dev/fake", lambda fix: None, opener=opener, retry_s=0.05)
    reader.start()
    try:
        _wait(lambda: len(opened) >= 2)
        assert reader.status()["reconnects"] >= 1
    finally:
        reader.stop()


def test_a_port_that_cannot_be_opened_is_an_error_state_not_an_exception() -> None:
    def opener(path: str) -> _FakeSource:
        raise PermissionError("permission denied (add the user to 'dialout')")

    reader = GnssReader("/dev/ttyACM0", lambda fix: None, opener=opener, retry_s=0.05)
    reader.start()
    try:
        _wait(lambda: reader.status()["state"] == "error")
        assert "dialout" in reader.status()["error"]
    finally:
        reader.stop()


# -- through gpsd ----------------------------------------------------------------

import json  # noqa: E402
import socket  # noqa: E402
import threading  # noqa: E402

from dmr_iq_surveyor.capture.gnss import (  # noqa: E402
    parse_gpsd_address,
    parse_gpsd_sky,
    parse_gpsd_tpv,
)

TPV = {"class": "TPV", "mode": 3, "lat": 32.0687, "lon": 34.8094, "speed": 13.9,
       "track": 90.5, "eph": 4.2, "altMSL": 41.0, "time": "2026-11-10T16:00:00.000Z"}
SKY = {"class": "SKY", "uSat": 8, "nSat": 12, "hdop": 0.9}


def test_gpsd_addresses() -> None:
    assert parse_gpsd_address("gpsd") == ("127.0.0.1", 2947)
    assert parse_gpsd_address("gpsd://10.0.0.5") == ("10.0.0.5", 2947)
    assert parse_gpsd_address("gpsd://localhost:3000") == ("localhost", 3000)
    assert parse_gpsd_address("/dev/ttyACM0") is None


def test_gpsd_tpv_and_sky_parse() -> None:
    fix = parse_gpsd_tpv(TPV)
    assert fix is not None and fix.latitude == 32.0687 and fix.accuracy_m == 4.2
    assert fix.speed_mps == 13.9 and fix.altitude_m == 41.0
    assert parse_gpsd_tpv({**TPV, "mode": 1}) is None, "mode 1 is no fix"
    assert parse_gpsd_tpv({**TPV, "lat": 0.0, "lon": 0.0}) is None, "(0, 0) is not a position"
    assert parse_gpsd_tpv({**TPV, "eph": None, "epx": 3.0, "epy": 5.0}).accuracy_m == 5.0
    assert parse_gpsd_sky(SKY) == (8, 0.9)
    listed = {"class": "SKY", "satellites": [{"used": True}, {"used": False}, {"used": True}]}
    assert parse_gpsd_sky(listed) == (2, None)


def test_auto_prefers_gpsd_when_it_is_running() -> None:
    assert resolve_device(AUTO_DEVICE, gpsd_probe=lambda: True) == "gpsd://127.0.0.1:2947"


class _FakeGpsd:
    """A TCP server that answers ?WATCH with gpsd-style JSON lines."""

    def __init__(self, messages: list[dict]) -> None:
        self.server = socket.socket()
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(1)
        self.port = self.server.getsockname()[1]
        self.watch_received = threading.Event()
        self._messages = messages
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        connection, _ = self.server.accept()
        with connection:
            request = connection.recv(1024)
            if b"?WATCH" in request:
                self.watch_received.set()
            for message in self._messages:
                connection.sendall((json.dumps(message) + "\n").encode())
            time.sleep(5)

    def close(self) -> None:
        self.server.close()


def test_reader_takes_fixes_from_gpsd_without_touching_the_port() -> None:
    gpsd = _FakeGpsd([{"class": "VERSION", "release": "3.25"},
                      {"class": "DEVICES", "devices": [{"path": "/dev/dmr-gps"}]},
                      SKY, {"class": "TPV", "mode": 1}, TPV])
    fixes: list[GnssFix] = []
    reader = GnssReader(f"gpsd://127.0.0.1:{gpsd.port}", fixes.append)
    reader.start()
    try:
        _wait(lambda: fixes)
        assert gpsd.watch_received.is_set()
        assert fixes[0].latitude == 32.0687 and fixes[0].satellites == 8
        status = reader.status()
        assert status["state"] == "fix" and status["hdop"] == 0.9
        assert status["device"] == f"gpsd://127.0.0.1:{gpsd.port}"
    finally:
        reader.stop()
        gpsd.close()


def test_gpsd_without_a_device_is_searching_not_an_error() -> None:
    gpsd = _FakeGpsd([{"class": "DEVICES", "devices": []}])
    reader = GnssReader(f"gpsd://127.0.0.1:{gpsd.port}", lambda fix: None)
    reader.start()
    try:
        # "searching" is also the state before the first connect, so wait for
        # the reason gpsd gave rather than for the state alone.
        _wait(lambda: "no GNSS device" in reader.status()["error"])
        assert reader.status()["state"] == "searching"
    finally:
        reader.stop()
        gpsd.close()


def test_a_busy_port_names_gpsd_as_the_way_out() -> None:
    def opener(path: str) -> _FakeSource:
        raise OSError(16, "Device or resource busy")

    reader = GnssReader("/dev/ttyACM0", lambda fix: None, opener=opener, retry_s=0.05)
    reader.start()
    try:
        _wait(lambda: reader.status()["state"] == "error")
        assert "--gnss-device gpsd" in reader.status()["error"]
    finally:
        reader.stop()
