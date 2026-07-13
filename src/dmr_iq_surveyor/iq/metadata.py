from __future__ import annotations

import re
import struct
from datetime import datetime, timezone
from pathlib import Path

from dmr_iq_surveyor.models import AuxiInfo, ChunkInfo, Ds64Info, FmtInfo, RecordingInfo

_U32_MAX = 0xFFFFFFFF


class WaveIQError(ValueError):
    """Raised when the recording is not a supported RIFF/RF64 IQ container."""


def _combine_u32(low: int, high: int) -> int:
    return (high << 32) | low


def _safe_system_time(values: tuple[int, ...]) -> str | None:
    # Windows SYSTEMTIME: year, month, day_of_week, day, hour, minute, second, ms
    try:
        year, month, _dow, day, hour, minute, second, milliseconds = values
        if year == 0:
            return None
        value = datetime(
            year,
            month,
            day,
            hour,
            minute,
            second,
            milliseconds * 1000,
            tzinfo=timezone.utc,
        )
        return value.isoformat()
    except (TypeError, ValueError, OverflowError):
        return None


def parse_ds64(payload: bytes) -> Ds64Info:
    if len(payload) < 28:
        raise WaveIQError("The ds64 chunk is shorter than 28 bytes")
    values = struct.unpack_from("<7I", payload, 0)
    riff_size = _combine_u32(values[0], values[1])
    data_size = _combine_u32(values[2], values[3])
    sample_count = _combine_u32(values[4], values[5])
    table_length = values[6]
    table: dict[str, int] = {}
    offset = 28
    for _ in range(table_length):
        if offset + 12 > len(payload):
            break
        chunk_id_raw, size = struct.unpack_from("<4sQ", payload, offset)
        table[chunk_id_raw.decode("ascii", errors="replace")] = size
        offset += 12
    return Ds64Info(
        riff_size=riff_size,
        data_size=data_size,
        sample_count=sample_count,
        table_length=table_length,
        table=table,
    )


def parse_fmt(payload: bytes) -> FmtInfo:
    if len(payload) < 16:
        raise WaveIQError("The fmt chunk is shorter than 16 bytes")
    format_code, channels, sample_rate, byte_rate, block_align, bits = struct.unpack_from(
        "<HHIIHH", payload, 0
    )
    extension_size = None
    valid_bits = None
    channel_mask = None
    subformat_guid_hex = None
    effective_format_code = format_code

    if len(payload) >= 18:
        extension_size = struct.unpack_from("<H", payload, 16)[0]

    # WAVE_FORMAT_EXTENSIBLE. The first two bytes of the subformat GUID contain
    # the effective PCM (1) or IEEE float (3) format code.
    if format_code == 0xFFFE and len(payload) >= 40:
        valid_bits, channel_mask = struct.unpack_from("<HI", payload, 18)
        subformat = payload[24:40]
        subformat_guid_hex = subformat.hex()
        effective_format_code = struct.unpack_from("<H", subformat, 0)[0]

    return FmtInfo(
        format_code=format_code,
        effective_format_code=effective_format_code,
        channels=channels,
        sample_rate_hz=sample_rate,
        byte_rate=byte_rate,
        block_align=block_align,
        bits_per_sample=bits,
        extension_size=extension_size,
        valid_bits_per_sample=valid_bits,
        channel_mask=channel_mask,
        subformat_guid_hex=subformat_guid_hex,
    )


def parse_auxi(payload: bytes) -> AuxiInfo:
    # SDRplay/SDRuno/SDRconnect layout documented by the rsp-recorder utility:
    # 16 SYSTEMTIME uint16 values, 9 uint32 values, 96-byte next-file field.
    if len(payload) == 164:
        values = struct.unpack("<8H8H9I96s", payload)
        next_file = values[25].split(b"\x00", 1)[0].decode("utf-8", errors="replace") or None
        return AuxiInfo(
            layout="sdrplay-164",
            start_time_utc=_safe_system_time(tuple(values[0:8])),
            stop_time_utc=_safe_system_time(tuple(values[8:16])),
            center_frequency_hz=values[16],
            ad_frequency_hz=values[17],
            if_frequency_hz=values[18],
            bandwidth_hz=values[19],
            iq_offset=values[20],
            db_offset_raw=values[21],
            max_value=values[22],
            next_file=next_file,
        )

    if len(payload) == 68:
        values = struct.unpack("<8H8H9I", payload)
        return AuxiInfo(
            layout="sdrplay-68",
            start_time_utc=_safe_system_time(tuple(values[0:8])),
            stop_time_utc=_safe_system_time(tuple(values[8:16])),
            center_frequency_hz=values[16],
            ad_frequency_hz=values[17],
            if_frequency_hz=values[18],
            bandwidth_hz=values[19],
            iq_offset=values[20],
            db_offset_raw=values[21],
            max_value=values[22],
        )

    if payload.startswith("<?xml ".encode("utf-16-le")):
        return AuxiInfo(
            layout="sdrconsole-xml",
            raw_xml=payload.decode("utf-16-le", errors="replace").rstrip("\x00"),
        )

    return AuxiInfo(layout=f"unknown-{len(payload)}-bytes")


def _encoding_name(fmt: FmtInfo) -> str:
    if fmt.effective_format_code == 1:
        if fmt.bits_per_sample == 8:
            return "unsigned_integer_pcm"
        return "signed_integer_pcm"
    if fmt.effective_format_code == 3:
        return "ieee_float"
    return f"unknown_wave_format_{fmt.effective_format_code}"


def _center_frequency_from_filename(path: Path) -> int | None:
    """Extract an SDRconnect-style center frequency suffix such as _163671500HZ."""
    match = re.search(r"(?:^|[_-])(\d{5,12})\s*HZ(?:$|[_.-])", path.name, re.IGNORECASE)
    if match is None:
        return None
    value = int(match.group(1))
    return value if value > 0 else None


def inspect_wave_iq(path: str | Path, assumed_iq_order: str = "IQ") -> RecordingInfo:
    source = Path(path).expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    file_size = source.stat().st_size
    warnings: list[str] = []
    chunks: list[ChunkInfo] = []
    ds64: Ds64Info | None = None
    fmt: FmtInfo | None = None
    auxi: AuxiInfo | None = None
    data_offset: int | None = None
    data_declared_size: int | None = None
    data_available_size: int | None = None

    with source.open("rb") as handle:
        header = handle.read(12)
        if len(header) != 12:
            raise WaveIQError("File is too short to contain a RIFF/RF64 header")
        container_raw, riff_size_u32, wave_raw = struct.unpack("<4sI4s", header)
        if container_raw not in {b"RIFF", b"RF64"} or wave_raw != b"WAVE":
            raise WaveIQError("Not a RIFF/RF64 WAVE file")
        container = container_raw.decode("ascii")

        while handle.tell() + 8 <= file_size:
            header_offset = handle.tell()
            chunk_header = handle.read(8)
            if len(chunk_header) < 8:
                break
            chunk_id_raw, declared_size = struct.unpack("<4sI", chunk_header)
            chunk_id = chunk_id_raw.decode("ascii", errors="replace")
            payload_offset = handle.tell()

            if declared_size == _U32_MAX:
                if chunk_id == "data" and ds64 is not None:
                    effective_size = ds64.data_size
                elif ds64 is not None and chunk_id in ds64.table:
                    effective_size = ds64.table[chunk_id]
                else:
                    effective_size = max(0, file_size - payload_offset)
                    warnings.append(
                        f"Chunk {chunk_id!r} has RF64 placeholder size but no matching ds64 size"
                    )
            else:
                effective_size = declared_size

            available = max(0, file_size - payload_offset)
            truncated = effective_size > available
            chunks.append(
                ChunkInfo(
                    chunk_id=chunk_id,
                    header_offset=header_offset,
                    data_offset=payload_offset,
                    declared_size=declared_size,
                    effective_size=effective_size,
                    truncated=truncated,
                )
            )

            if chunk_id == "data":
                data_offset = payload_offset
                data_declared_size = effective_size
                data_available_size = min(effective_size, available)
                if truncated:
                    warnings.append(
                        f"The data chunk declares {effective_size} bytes but only {available} are available"
                    )
                break

            if truncated:
                warnings.append(f"Chunk {chunk_id!r} extends beyond end of file")
                payload_size_to_read = available
            else:
                payload_size_to_read = effective_size

            # Metadata chunks are small. Protect against malformed files that claim
            # an unexpectedly huge non-data chunk.
            if payload_size_to_read > 16 * 1024 * 1024:
                warnings.append(
                    f"Skipped unusually large metadata chunk {chunk_id!r} ({payload_size_to_read} bytes)"
                )
                handle.seek(payload_offset + payload_size_to_read + (payload_size_to_read & 1))
                continue

            payload = handle.read(payload_size_to_read)
            if chunk_id == "ds64":
                ds64 = parse_ds64(payload)
            elif chunk_id == "fmt ":
                fmt = parse_fmt(payload)
            elif chunk_id == "auxi":
                auxi = parse_auxi(payload)

            next_offset = payload_offset + effective_size + (effective_size & 1)
            if next_offset > file_size:
                break
            handle.seek(next_offset)

    if fmt is None:
        raise WaveIQError("No fmt chunk was found")
    if data_offset is None or data_declared_size is None or data_available_size is None:
        raise WaveIQError("No data chunk was found")
    if fmt.channels < 2:
        warnings.append(f"Expected at least two channels for IQ, found {fmt.channels}")
    if fmt.block_align <= 0:
        raise WaveIQError("Invalid block alignment")
    if data_available_size % fmt.block_align:
        warnings.append(
            "Available data size is not an exact multiple of the frame block alignment; trailing bytes ignored"
        )

    frame_count = data_available_size // fmt.block_align
    duration = frame_count / fmt.sample_rate_hz if fmt.sample_rate_hz else 0.0
    metadata_center = auxi.center_frequency_hz if auxi else None
    filename_center = _center_frequency_from_filename(source)
    if metadata_center is not None:
        center = metadata_center
        center_source = "auxi"
        if filename_center is not None and filename_center != metadata_center:
            warnings.append(
                f"Center frequency in filename ({filename_center}) differs from auxi metadata ({metadata_center})"
            )
    elif filename_center is not None:
        center = filename_center
        center_source = "filename"
        warnings.append(
            "Center frequency was missing from container metadata and was derived from the filename"
        )
    else:
        center = None
        center_source = "missing"
        warnings.append("Center frequency is unavailable in both metadata and filename")

    low = center - fmt.sample_rate_hz / 2 if center is not None else None
    high = center + fmt.sample_rate_hz / 2 if center is not None else None

    if container == "RF64" and ds64 is None:
        warnings.append("RF64 container has no ds64 chunk")
    if ds64 and ds64.sample_count not in {0, frame_count}:
        warnings.append(
            f"ds64 sample count ({ds64.sample_count}) differs from calculated frame count ({frame_count})"
        )
    expected_byte_rate = fmt.sample_rate_hz * fmt.block_align
    if fmt.byte_rate != expected_byte_rate:
        warnings.append(
            f"fmt byte rate ({fmt.byte_rate}) differs from sample_rate × block_align ({expected_byte_rate})"
        )

    iq_order = assumed_iq_order.upper()
    if iq_order not in {"IQ", "QI"}:
        raise WaveIQError("assumed_iq_order must be IQ or QI")

    return RecordingInfo(
        path=str(source),
        file_size_bytes=file_size,
        container=container,
        wave_format=wave_raw.decode("ascii"),
        riff_declared_size=ds64.riff_size if container == "RF64" and ds64 else riff_size_u32,
        chunks=chunks,
        fmt=fmt,
        ds64=ds64,
        auxi=auxi,
        data_offset_bytes=data_offset,
        data_declared_size_bytes=data_declared_size,
        data_available_size_bytes=data_available_size,
        frame_count=frame_count,
        duration_seconds=duration,
        center_frequency_hz=center,
        center_frequency_source=center_source,
        nominal_frequency_low_hz=low,
        nominal_frequency_high_hz=high,
        sample_encoding=_encoding_name(fmt),
        iq_order=iq_order,
        iq_order_confidence="assumed_from_SDRconnect_channel_convention_not_proven_from_statistics",
        warnings=warnings,
    )
