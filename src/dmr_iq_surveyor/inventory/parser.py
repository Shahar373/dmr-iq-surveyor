from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

ANSI_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
CLOCK_RE = re.compile(r"^(?P<clock>\d{2}:\d{2}:\d{2})\s+")
SYNC_RE = re.compile(r"\bSync:\s*(?P<polarity>[+-])DMR\b", re.IGNORECASE)
ACTIVE_SLOT_RE = re.compile(r"\[\s*slot\s*(?P<slot>[12])\s*\]", re.IGNORECASE)
COLOR_CODE_RE = re.compile(r"Color\s+Code\s*[=:]\s*(?P<cc>\d+|XX)", re.IGNORECASE)
TG_RE = re.compile(r"(?:Talkgroup|\bTG|\bTGT|Target)\s*[=:]\s*(?P<value>\d+)", re.IGNORECASE)
RID_RE = re.compile(r"(?:Radio\s+ID|Source|\bSRC)\s*[=:]\s*(?P<value>\d+)", re.IGNORECASE)
VC_RE = re.compile(r"\bVC(?P<stage>[1-6])\b", re.IGNORECASE)
ACTIVITY_RE = re.compile(
    r"Activity\s+Update\s+TS1:\s*(?P<ts1>.*?)(?:\s+TS2:\s*(?P<ts2>.*))?$",
    re.IGNORECASE,
)
MOTO_DATA_RE = re.compile(r"Moto\s+Data\s+Channel:\s*(?P<payload>.+)$", re.IGNORECASE)
LSN_RE = re.compile(r"\bLSN\s+\d+:", re.IGNORECASE)
ERROR_RE = re.compile(r"\b(?:ERR|ERROR|FAILED|FATAL|CRC|FEC|CACH)\b", re.IGNORECASE)


@dataclass(slots=True)
class ParsedEvent:
    line_index: int
    decoder_clock: str | None
    slot: int | None
    event_type: str
    event_subtype: str | None
    color_code: int | None
    talkgroup_id: int | None
    radio_id: int | None
    is_error: bool
    raw_line: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def strip_ansi(value: str) -> str:
    return ANSI_RE.sub("", value)


def _clock(line: str) -> str | None:
    match = CLOCK_RE.match(line)
    return match.group("clock") if match else None


def _color_code(line: str) -> int | None:
    match = COLOR_CODE_RE.search(line)
    if not match or match.group("cc").upper() == "XX":
        return None
    return int(match.group("cc"))


def _slot(line: str) -> int | None:
    match = ACTIVE_SLOT_RE.search(line)
    return int(match.group("slot")) if match else None


def _event_from_sync(line: str, line_index: int) -> ParsedEvent | None:
    if not SYNC_RE.search(line):
        return None
    lower = line.lower()
    subtype: str | None = None
    event_type = "sync"
    vc = VC_RE.search(line)
    if vc:
        event_type = "voice"
        subtype = f"vc{vc.group('stage')}"
    elif "group voice" in lower:
        event_type = "voice"
        subtype = "group_voice"
    elif "private voice" in lower:
        event_type = "voice"
        subtype = "private_voice"
    elif re.search(r"\bvoice\b", line, re.IGNORECASE):
        event_type = "voice"
        subtype = "voice"
    elif re.search(r"\bcsbk\b", line, re.IGNORECASE):
        event_type = "control"
        subtype = "csbk"
    elif re.search(r"\bdata\b", line, re.IGNORECASE):
        event_type = "data"
        subtype = "data"
    elif re.search(r"\bidle\b", line, re.IGNORECASE):
        event_type = "idle"
        subtype = "idle"
    elif ERROR_RE.search(line):
        event_type = "error"
        subtype = "decoder_error"
    return ParsedEvent(
        line_index=line_index,
        decoder_clock=_clock(line),
        slot=_slot(line),
        event_type=event_type,
        event_subtype=subtype,
        color_code=_color_code(line),
        talkgroup_id=None,
        radio_id=None,
        is_error=bool(ERROR_RE.search(line)),
        raw_line=line,
    )


def _activity_events(line: str, line_index: int) -> list[ParsedEvent]:
    match = ACTIVITY_RE.search(line)
    if not match:
        return []
    events: list[ParsedEvent] = []
    for slot, field in ((1, "ts1"), (2, "ts2")):
        value = (match.group(field) or "").strip(" ;|")
        if not value:
            continue
        lower = value.lower()
        event_type = "activity"
        subtype = lower.replace(" ", "_")
        if "voice" in lower:
            event_type = "voice"
        elif "data" in lower:
            event_type = "data"
        elif "idle" in lower:
            event_type = "idle"
        events.append(
            ParsedEvent(
                line_index=line_index,
                decoder_clock=_clock(line),
                slot=slot,
                event_type=event_type,
                event_subtype=subtype,
                color_code=_color_code(line),
                talkgroup_id=None,
                radio_id=None,
                is_error=bool(ERROR_RE.search(line)),
                raw_line=line,
            )
        )
    return events


def parse_log_lines(lines: Iterable[str]) -> list[ParsedEvent]:
    events: list[ParsedEvent] = []
    for line_index, raw in enumerate(lines, start=1):
        line = strip_ansi(raw.rstrip("\n\r")).strip()
        if not line:
            continue
        sync = _event_from_sync(line, line_index)
        if sync is not None:
            events.append(sync)
            continue
        activity = _activity_events(line, line_index)
        if activity:
            events.extend(activity)
            continue
        tg_values = [int(match.group("value")) for match in TG_RE.finditer(line)]
        rid_values = [int(match.group("value")) for match in RID_RE.finditer(line)]
        if tg_values or rid_values:
            values = max(len(tg_values), len(rid_values), 1)
            for index in range(values):
                events.append(
                    ParsedEvent(
                        line_index=line_index,
                        decoder_clock=_clock(line),
                        slot=_slot(line),
                        event_type="identity",
                        event_subtype="talkgroup_or_radio",
                        color_code=_color_code(line),
                        talkgroup_id=(
                            tg_values[index] if index < len(tg_values) else None
                        ),
                        radio_id=(
                            rid_values[index] if index < len(rid_values) else None
                        ),
                        is_error=bool(ERROR_RE.search(line)),
                        raw_line=line,
                    )
                )
            continue
        if MOTO_DATA_RE.search(line):
            events.append(
                ParsedEvent(
                    line_index=line_index,
                    decoder_clock=_clock(line),
                    slot=_slot(line),
                    event_type="vendor_data",
                    event_subtype="motorola_data_channel",
                    color_code=_color_code(line),
                    talkgroup_id=None,
                    radio_id=None,
                    is_error=bool(ERROR_RE.search(line)),
                    raw_line=line,
                )
            )
            continue
        if LSN_RE.search(line):
            events.append(
                ParsedEvent(
                    line_index=line_index,
                    decoder_clock=_clock(line),
                    slot=None,
                    event_type="network_state",
                    event_subtype="logical_slot_status",
                    color_code=_color_code(line),
                    talkgroup_id=None,
                    radio_id=None,
                    is_error=bool(ERROR_RE.search(line)),
                    raw_line=line,
                )
            )
    return events


def parse_log_file(path: str | Path) -> list[ParsedEvent]:
    source = Path(path)
    return parse_log_lines(
        source.read_text(encoding="utf-8", errors="replace").splitlines()
    )
