"""Streaming RF64/WAVE writer for live IQ capture.

Frames are written to disk incrementally in caller-sized chunks -- nothing
here ever holds a full capture in memory, matching this project's
memory-bounded discipline (`iq/reader.py`'s memmap reads, `decode/core.py`'s
`process_complex_chunks`) applied to writing instead of reading.

The container is always RF64 (RIFF64 with a `ds64` chunk), never plain
RIFF. A 60-120 second capture at 10 MS/s is 3.6-4.8 GB of interleaved 16-bit
I/Q; the top of that range exceeds the 32-bit `data` chunk size field
(2**32 - 1 bytes, ~4.29 GB), which is exactly the field capture duration
this module was built for. `iq/metadata.py::inspect_wave_iq` already parses
RF64/`ds64` (real oversized SDRconnect recordings use it too), so writing
RF64 unconditionally sidesteps the boundary instead of switching container
formats mid-file or capping duration.

An `auxi` chunk in the `sdrplay-164` layout parsed by
`iq/metadata.py::parse_auxi` is written whenever `write_auxi=True` (the
default). This is the "auxi vs filename-fallback" judgment call from the
task: writing a real auxi chunk makes `inspect_wave_iq()` resolve
`center_frequency_source == "auxi"` exactly like a genuine SDRplay
recording, which is strictly more useful provenance than relying solely on
the SDRconnect filename convention -- and the byte layout is fully
determined by `parse_auxi`'s `struct.unpack("<8H8H9I96s", payload)`, so
there is no ambiguity to get subtly wrong. Only two auxi fields are this
capture's actual ground truth: `center_frequency_hz` (the requested tuner
frequency) and the SYSTEMTIME start/stop pair (wall-clock capture bounds).
The rest of the `sdrplay-164` layout describes receiver-internal values
(A/D rate, IF frequency, IF bandwidth, IQ balance offsets) that a
SoapySDR-level capture has no way to read back from the RSP1B, so they are
filled with clearly-documented best-effort placeholders below rather than
invented measurements; `iq/metadata.py` never reads them for anything
besides display, so this is inert either way. `--no-write-auxi` remains
available as an escape hatch onto the proven filename fallback
(`survey/discovery.py::resolve_capture_time`, `_FILENAME_TIMESTAMP_RE`) if
the auxi chunk is ever suspected of confusing some other tool.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO

import numpy as np

_FMT_PAYLOAD_SIZE = 16
_DS64_PAYLOAD_SIZE = 28
_AUXI_PAYLOAD_SIZE = 164
_U32_MAX = 0xFFFFFFFF
_PCM16_FULL_SCALE = 32767.0


def _system_time(moment: datetime) -> tuple[int, ...]:
    # Windows SYSTEMTIME layout expected by iq/metadata.py::_safe_system_time:
    # year, month, day_of_week, day, hour, minute, second, milliseconds.
    # day_of_week is never read by _safe_system_time and is written as 0.
    return (
        moment.year,
        moment.month,
        0,
        moment.day,
        moment.hour,
        moment.minute,
        moment.second,
        moment.microsecond // 1000,
    )


def _auxi_payload(
    *,
    center_frequency_hz: int,
    sample_rate_hz: int,
    start: datetime,
    stop: datetime,
) -> bytes:
    values = (
        *_system_time(start),
        *_system_time(stop),
        int(center_frequency_hz),
        int(sample_rate_hz),  # ad_frequency_hz: best-effort, RSP1B direct-samples at the requested rate
        0,  # if_frequency_hz: zero-IF direct sampling is SoapySDRPlay3's default tuning mode
        int(sample_rate_hz),  # bandwidth_hz: best-effort, no SoapySDR API exposes the applied filter width
        0,  # iq_offset: not measured by a SoapySDR-level capture
        0,  # db_offset_raw: not measured by a SoapySDR-level capture
        int(_PCM16_FULL_SCALE),  # max_value: full-scale int16 magnitude, matches the encoding actually written
        0,
        0,
        b"\x00" * 96,  # next_file: unused for a single-file capture
    )
    return struct.pack("<8H8H9I96s", *values)


def _chunk_header(chunk_id: bytes, size: int) -> bytes:
    return chunk_id + struct.pack("<I", size)


@dataclass(slots=True)
class WaveIQWriterSettings:
    sample_rate_hz: int
    center_frequency_hz: int
    write_auxi: bool = True

    def validate(self) -> None:
        if self.sample_rate_hz <= 0:
            raise ValueError("sample_rate_hz must be positive")


class WaveIQWriter:
    """Streams interleaved signed 16-bit I/Q frames to an RF64/WAVE file.

    Usable as a context manager. `write_frames()` may be called any number
    of times with bounded-size chunks; `close()` (called automatically by
    `__exit__`) patches the `ds64` sizes and the auxi stop time -- the only
    parts of the file that need the final frame count, so only a small
    fixed-size header region is rewritten, never the sample data.
    """

    def __init__(self, path: str | Path, settings: WaveIQWriterSettings) -> None:
        settings.validate()
        self.settings = settings
        self.path = Path(path)
        self._frame_count = 0
        self._start_utc = datetime.now(UTC)
        self._closed = False
        self._handle: BinaryIO = self.path.open("wb")
        self._auxi_offset: int | None = None
        self._write_header()

    def _write_header(self) -> None:
        handle = self._handle
        # RF64 container: 'RF64', a placeholder size (0xFFFFFFFF is the
        # documented RF64 marker; iq/metadata.py does not gate on this
        # field, but writing it keeps the file spec-conformant for other
        # RF64 readers), 'WAVE'.
        handle.write(b"RF64" + struct.pack("<I", _U32_MAX) + b"WAVE")
        # ds64: authoritative 64-bit riff/data/sample-count sizes, patched
        # in close() once the final frame count is known. table_length=0 --
        # no oversized non-data chunks are ever written by this module.
        self._ds64_offset = handle.tell()
        handle.write(_chunk_header(b"ds64", _DS64_PAYLOAD_SIZE))
        handle.write(struct.pack("<7I", 0, 0, 0, 0, 0, 0, 0))

        fmt_payload = struct.pack(
            "<HHIIHH",
            1,  # format_code: PCM
            2,  # channels: I, Q
            int(self.settings.sample_rate_hz),
            int(self.settings.sample_rate_hz) * 4,  # byte_rate = sample_rate * block_align
            4,  # block_align: 2 channels * 2 bytes
            16,  # bits_per_sample
        )
        handle.write(_chunk_header(b"fmt ", _FMT_PAYLOAD_SIZE))
        handle.write(fmt_payload)

        if self.settings.write_auxi:
            self._auxi_offset = handle.tell()
            handle.write(_chunk_header(b"auxi", _AUXI_PAYLOAD_SIZE))
            handle.write(
                _auxi_payload(
                    center_frequency_hz=self.settings.center_frequency_hz,
                    sample_rate_hz=self.settings.sample_rate_hz,
                    start=self._start_utc,
                    stop=self._start_utc,
                )
            )

        # data chunk header: declared size 0xFFFFFFFF per RF64 -- the real
        # size lives in ds64.data_size, exactly as inspect_wave_iq() expects
        # (`if chunk_id == "data" and ds64 is not None: effective_size = ds64.data_size`).
        handle.write(b"data" + struct.pack("<I", _U32_MAX))
        self._data_offset = handle.tell()

    def write_frames(self, iq: np.ndarray) -> None:
        """Append one chunk of complex samples as interleaved signed 16-bit
        I/Q. Callers must pass bounded-size chunks -- never the whole
        capture at once (see `capture/core.py::run_capture`)."""
        if iq.size == 0:
            return
        i_values = np.clip(np.real(iq) * _PCM16_FULL_SCALE, -32768, 32767).astype("<i2")
        q_values = np.clip(np.imag(iq) * _PCM16_FULL_SCALE, -32768, 32767).astype("<i2")
        interleaved = np.empty(iq.size * 2, dtype="<i2")
        interleaved[0::2] = i_values
        interleaved[1::2] = q_values
        self._handle.write(interleaved.tobytes())
        self._frame_count += iq.size

    @property
    def frame_count(self) -> int:
        return self._frame_count

    def close(self) -> dict[str, Any]:
        if self._closed:
            raise ValueError("WaveIQWriter is already closed")
        self._closed = True
        handle = self._handle
        data_size = self._frame_count * 4
        if data_size & 1:
            handle.write(b"\x00")
        stop_utc = datetime.now(UTC)

        if self._auxi_offset is not None:
            handle.seek(self._auxi_offset + 8)
            handle.write(
                _auxi_payload(
                    center_frequency_hz=self.settings.center_frequency_hz,
                    sample_rate_hz=self.settings.sample_rate_hz,
                    start=self._start_utc,
                    stop=stop_utc,
                )
            )
            handle.seek(0, 2)

        file_size = handle.tell()
        riff_size = file_size - 8
        handle.seek(self._ds64_offset + 8)
        handle.write(
            struct.pack(
                "<7I",
                riff_size & _U32_MAX,
                riff_size >> 32,
                data_size & _U32_MAX,
                data_size >> 32,
                self._frame_count & _U32_MAX,
                self._frame_count >> 32,
                0,
            )
        )
        handle.close()
        return {
            "path": str(self.path),
            "frame_count": self._frame_count,
            "data_size_bytes": data_size,
            "file_size_bytes": file_size,
            "start_utc": self._start_utc.isoformat(),
            "stop_utc": stop_utc.isoformat(),
        }

    def __enter__(self) -> WaveIQWriter:
        return self

    def __exit__(self, *exc_info: object) -> None:
        if not self._closed:
            self.close()


__all__ = ["WaveIQWriter", "WaveIQWriterSettings"]
