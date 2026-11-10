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
