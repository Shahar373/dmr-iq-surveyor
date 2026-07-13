from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(slots=True)
class ChunkInfo:
    chunk_id: str
    header_offset: int
    data_offset: int
    declared_size: int
    effective_size: int
    truncated: bool = False


@dataclass(slots=True)
class Ds64Info:
    riff_size: int
    data_size: int
    sample_count: int
    table_length: int
    table: dict[str, int] = field(default_factory=dict)


@dataclass(slots=True)
class FmtInfo:
    format_code: int
    effective_format_code: int
    channels: int
    sample_rate_hz: int
    byte_rate: int
    block_align: int
    bits_per_sample: int
    extension_size: int | None = None
    valid_bits_per_sample: int | None = None
    channel_mask: int | None = None
    subformat_guid_hex: str | None = None


@dataclass(slots=True)
class AuxiInfo:
    layout: str
    start_time_utc: str | None = None
    stop_time_utc: str | None = None
    center_frequency_hz: int | None = None
    ad_frequency_hz: int | None = None
    if_frequency_hz: int | None = None
    bandwidth_hz: int | None = None
    iq_offset: int | None = None
    db_offset_raw: int | None = None
    max_value: int | None = None
    next_file: str | None = None
    raw_xml: str | None = None


@dataclass(slots=True)
class RecordingInfo:
    path: str
    file_size_bytes: int
    container: str
    wave_format: str
    riff_declared_size: int
    chunks: list[ChunkInfo]
    fmt: FmtInfo
    ds64: Ds64Info | None
    auxi: AuxiInfo | None
    data_offset_bytes: int
    data_declared_size_bytes: int
    data_available_size_bytes: int
    frame_count: int
    duration_seconds: float
    center_frequency_hz: int | None
    center_frequency_source: str
    nominal_frequency_low_hz: float | None
    nominal_frequency_high_hz: float | None
    sample_encoding: str
    iq_order: str
    iq_order_confidence: str
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
