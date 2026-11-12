"""The USB GNSS reader against the sentence order a u-blox 7 really sends,
and against sources that are not terminals or cannot be opened at all.

A u-blox 7 (the VK-162 on the field Pi) emits, every second:
RMC, VTG, GGA, GSA, GSV..., GLL. RMC comes *before* the GGA of the same
epoch, so an assembler that only looks back at the last GGA pairs every fix
with the previous second's GGA, or with none.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Iterator

import pytest

from dmr_iq_surveyor.capture.gnss import (
    FixAssembler,
    GnssFix,
    GnssReader,
    SerialLineSource,
    default_opener,
    parse_nmea,
)


def nmea(body: str) -> str:
    checksum = 0
    for character in body:
        checksum ^= ord(character)
    return f"${body}*{checksum:02X}"


def rmc(t: str, *, valid: bool = True) -> str:
    if not valid:
        return nmea(f"GPRMC,{t},V,,,,,,,101126,,,N")
    return nmea(f"GPRMC,{t},A,3204.1234,N,03448.5678,E,27.0,90.5,101126,,,A")


def vtg() -> str:
    return nmea("GPVTG,90.5,T,,M,27.0,N,50.0,K,A")


def gga(t: str, *, quality: int = 1, satellites: int = 9, hdop: float = 0.95) -> str:
    if quality == 0:
        return nmea(f"GPGGA,{t},,,,,0,00,99.99,,,,,,")
    return nmea(
        f"GPGGA,{t},3204.1234,N,03448.5678,E,{quality},{satellites:02d},{hdop},42.1,M,18.0,M,,"
    )


def ublox_epoch(t: str, **gga_fields) -> list[str]:
    """One second of a u-blox 7: RMC, VTG, GGA, GSA, GSV, GLL."""
    return [
        rmc(t),
        vtg(),
        gga(t, **gga_fields),
        nmea("GPGSA,A,3,04,05,09,12,,,,,,,,,1.8,0.95,1.5"),
        nmea("GPGSV,3,1,11,04,40,083,46,05,17,308,41,09,07,344,39,12,77,048,45"),
        nmea(f"GPGLL,3204.1234,N,03448.5678,E,{t},A,A"),
    ]


def feed_all(assembler: FixAssembler, lines: list[str]) -> list[GnssFix]:
    fixes = []
    for line in lines:
        sentence = parse_nmea(line)
        if sentence is None:
            continue
        fix = assembler.feed(sentence)
        if fix is not None:
            fixes.append(fix)
    return fixes


# -- FixAssembler, u-blox order ------------------------------------------------


def test_rmc_first_epoch_gets_its_own_gga_quality() -> None:
    assembler = FixAssembler()
    fixes = feed_all(assembler, ublox_epoch("083559.00", satellites=9, hdop=0.95))
    assert len(fixes) == 1
    fix = fixes[0]
    assert fix.time_utc == "083559.00"
    assert fix.satellites == 9 and fix.hdop == 0.95 and fix.altitude_m == 42.1


def test_rmc_is_held_until_the_same_epoch_gga_not_paired_with_the_previous_one() -> None:
    assembler = FixAssembler()
    assert feed_all(assembler, [rmc("083559.00"), vtg()]) == []  # held, not emitted bare
    fix = assembler.feed(parse_nmea(gga("083559.00", satellites=7, hdop=1.4)))
    assert fix is not None and fix.satellites == 7 and fix.hdop == 1.4


def test_every_second_of_a_ublox_stream_carries_that_seconds_gga() -> None:
    assembler = FixAssembler()
    lines: list[str] = []
    for second, satellites in ((59, 6), (60, 7), (61, 8)):
        t = f"0835{second:02d}.00" if second < 60 else f"0836{second - 60:02d}.00"
        lines += ublox_epoch(t, satellites=satellites)
    fixes = feed_all(assembler, lines)
    assert [f.satellites for f in fixes] == [6, 7, 8]
    assert [f.time_utc for f in fixes] == ["083559.00", "083600.00", "083601.00"]


def test_same_epoch_gga_quality_0_vetoes_an_rmc_that_came_first() -> None:
    assembler = FixAssembler()
    assert feed_all(assembler, ublox_epoch("083559.00", quality=0)) == []
    # ...and the veto is not carried into the next, good epoch.
    fixes = feed_all(assembler, ublox_epoch("083600.00"))
    assert len(fixes) == 1 and fixes[0].time_utc == "083600.00"


def test_a_previous_epochs_gga_does_not_veto_or_describe_this_rmc() -> None:
    assembler = FixAssembler()
    assembler.feed(parse_nmea(gga("083558.00", quality=0)))
    assert assembler.feed(parse_nmea(rmc("083559.00"))) is None  # held, not vetoed
    fix = assembler.feed(parse_nmea(gga("083559.00", satellites=5)))
    assert fix is not None and fix.satellites == 5


def test_a_held_rmc_whose_gga_never_comes_is_emitted_at_the_next_epoch() -> None:
    # An RMC-only stream (GGA disabled, or GGA lost to a checksum error).
    assembler = FixAssembler()
    assert assembler.feed(parse_nmea(rmc("083559.00"))) is None
    fix = assembler.feed(parse_nmea(rmc("083600.00")))
    assert fix is not None and fix.time_utc == "083559.00"
    assert fix.satellites is None and fix.hdop is None
    later = assembler.flush()
    assert later is not None and later.time_utc == "083600.00"
    assert assembler.flush() is None


def test_a_held_rmc_is_emitted_when_a_gga_for_another_time_arrives() -> None:
    assembler = FixAssembler()
    assembler.feed(parse_nmea(rmc("083559.00")))
    fix = assembler.feed(parse_nmea(gga("083600.00", satellites=3)))
    assert fix is not None and fix.time_utc == "083559.00" and fix.satellites is None


def test_a_held_rmc_is_emitted_when_the_next_rmc_says_no_fix() -> None:
    assembler = FixAssembler()
    assembler.feed(parse_nmea(rmc("083559.00")))
    fix = assembler.feed(parse_nmea(rmc("083600.00", valid=False)))
    assert fix is not None and fix.time_utc == "083559.00"
    assert assembler.flush() is None


def test_gga_first_order_still_emits_at_once() -> None:
    assembler = FixAssembler()
    assert assembler.feed(parse_nmea(gga("083559.00"))) is None
    fix = assembler.feed(parse_nmea(rmc("083559.00")))
    assert fix is not None and fix.satellites == 9
    assert assembler.flush() is None, "nothing left held"


# -- the reader loop with u-blox order -------------------------------------------


class _Source:
    def __init__(self, lines: list[str | None], *, idle: bool = True) -> None:
        self._lines = lines
        self._idle = idle
        self.closed = False

    def lines(self) -> Iterator[str | None]:
        yield from self._lines
        while self._idle:
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


def test_reader_fixes_from_a_ublox_stream_carry_satellites_and_hdop() -> None:
    fixes: list[GnssFix] = []
    lines = ublox_epoch("083559.00", satellites=9, hdop=0.95) + ublox_epoch(
        "083600.00", satellites=10, hdop=0.8
    )
    reader = GnssReader("/dev/fake", fixes.append, opener=lambda path: _Source(list(lines)))
    reader.start()
    try:
        _wait(lambda: len(fixes) >= 2)
        assert [(f.satellites, f.hdop) for f in fixes] == [(9, 0.95), (10, 0.8)]
        status = reader.status()
        assert status["state"] == "fix" and status["fixes"] == 2
        assert status["last_fix"]["satellites"] == 10
    finally:
        reader.stop()


def test_reader_honours_a_same_epoch_gga_veto_in_ublox_order() -> None:
    fixes: list[GnssFix] = []
    lines = ublox_epoch("083559.00", quality=0)
    reader = GnssReader("/dev/fake", fixes.append, opener=lambda path: _Source(list(lines)))
    reader.start()
    try:
        _wait(lambda: reader.status()["sentences"] >= len(lines))
        time.sleep(0.05)  # idle timeouts flush nothing: the RMC was vetoed
        assert fixes == []
        assert reader.status()["state"] == "no_fix"
    finally:
        reader.stop()


def test_reader_flushes_a_held_rmc_on_a_quiet_read_timeout() -> None:
    # An RMC-only receiver: the read timeout (a None line) ends the epoch.
    fixes: list[GnssFix] = []
    reader = GnssReader("/dev/fake", fixes.append, opener=lambda path: _Source([rmc("083559.00")]))
    reader.start()
    try:
        _wait(lambda: fixes)
        assert fixes[0].time_utc == "083559.00" and fixes[0].satellites is None
        assert reader.status()["state"] == "fix"
    finally:
        reader.stop()


def test_a_flushed_fix_does_not_hide_that_the_current_epoch_has_no_fix() -> None:
    fixes: list[GnssFix] = []
    lines = [rmc("083559.00"), rmc("083600.00", valid=False)]
    reader = GnssReader("/dev/fake", fixes.append, opener=lambda path: _Source(lines))
    reader.start()
    try:
        _wait(lambda: fixes and reader.status()["sentences"] >= 2)
        assert reader.status()["state"] == "no_fix"
    finally:
        reader.stop()


# -- sources that are not terminals, and openers that raise -----------------------


def test_a_fifo_is_read_as_is_not_refused(tmp_path) -> None:
    fifo = tmp_path / "gnss.fifo"
    os.mkfifo(fifo)
    writer_fd: list[int] = []

    def write() -> None:
        fd = os.open(fifo, os.O_WRONLY)
        writer_fd.append(fd)
        os.write(fd, ("\r\n".join(ublox_epoch("083559.00")) + "\r\n").encode())

    # O_NONBLOCK read-open of a FIFO succeeds without a writer.
    source = SerialLineSource(str(fifo), read_timeout_s=0.2)
    thread = threading.Thread(target=write)
    thread.start()
    try:
        thread.join(timeout=3)
        got = []
        for line in source.lines():
            if line is not None:
                got.append(line.strip())
            if len(got) == 6:
                break
        assert got[0].startswith("$GPRMC") and got[2].startswith("$GPGGA")
    finally:
        source.close()
        for fd in writer_fd:
            os.close(fd)


def test_a_regular_file_replay_is_read_and_its_descriptor_is_not_leaked(tmp_path) -> None:
    replay = tmp_path / "drive.nmea"
    replay.write_text("\r\n".join(ublox_epoch("083559.00")) + "\r\n")
    before = set(os.listdir("/proc/self/fd")) if os.path.isdir("/proc/self/fd") else None
    source = SerialLineSource(str(replay))
    lines = []
    with pytest.raises(OSError, match="end of stream"):
        for line in source.lines():
            if line is not None:
                lines.append(line)
    source.close()
    assert len(lines) == 6
    if before is not None:
        assert set(os.listdir("/proc/self/fd")) <= before


def test_an_unexpected_configure_failure_closes_the_descriptor(tmp_path, monkeypatch) -> None:
    path = tmp_path / "port"
    path.write_text("")
    opened: list[int] = []
    real_open = os.open

    def tracking_open(*args, **kwargs):
        fd = real_open(*args, **kwargs)
        opened.append(fd)
        return fd

    def broken(self) -> None:
        raise RuntimeError("driver oddity")

    monkeypatch.setattr(os, "open", tracking_open)
    monkeypatch.setattr(SerialLineSource, "_configure", broken)
    with pytest.raises(RuntimeError):
        SerialLineSource(str(path))
    assert opened
    with pytest.raises(OSError):
        os.fstat(opened[0])  # closed


def test_a_bad_gpsd_address_is_an_error_state_and_the_reader_keeps_retrying() -> None:
    with pytest.raises(ValueError):
        default_opener("gpsd://host:abc")
    reader = GnssReader("gpsd://host:abc", lambda fix: None, retry_s=0.05)
    reader.start()
    try:
        _wait(lambda: reader.status()["state"] == "error")
        assert "bad gpsd address" in reader.status()["error"]
        assert reader._thread is not None and reader._thread.is_alive()
    finally:
        reader.stop()


def test_any_opener_exception_is_retried_and_a_later_open_succeeds() -> None:
    attempts: list[int] = []
    fixes: list[GnssFix] = []

    def opener(path: str) -> _Source:
        attempts.append(1)
        if len(attempts) < 3:
            raise RuntimeError("not yet")
        return _Source(ublox_epoch("083559.00"))

    reader = GnssReader("/dev/fake", fixes.append, opener=opener, retry_s=0.02)
    reader.start()
    try:
        _wait(lambda: fixes)
        assert len(attempts) == 3
        assert reader.status()["state"] == "fix" and reader.status()["error"] == ""
    finally:
        reader.stop()
