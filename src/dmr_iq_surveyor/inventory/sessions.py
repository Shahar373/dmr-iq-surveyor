from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Iterable

from dmr_iq_surveyor.inventory.parser import ParsedEvent


@dataclass(slots=True)
class EventSession:
    session_index: int
    slot: int | None
    session_type: str
    start_line: int
    end_line: int
    start_clock: str | None
    end_clock: str | None
    duration_seconds_estimate: float | None
    timing_confidence: str
    event_count: int
    error_count: int
    dominant_color_code: int | None
    talkgroup_ids: list[int]
    radio_ids: list[int]
    event_subtypes: list[str]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _clock_seconds(value: str | None) -> int | None:
    if not value:
        return None
    parsed = datetime.strptime(value, "%H:%M:%S")
    return parsed.hour * 3600 + parsed.minute * 60 + parsed.second


def _session_type(events: list[ParsedEvent]) -> str:
    types = {item.event_type for item in events}
    if "voice" in types:
        return "voice"
    if "data" in types or "vendor_data" in types:
        return "data"
    if "control" in types or "network_state" in types:
        return "control"
    if types == {"idle"}:
        return "idle"
    return "mixed"


def _build_session(index: int, items: list[ParsedEvent]) -> EventSession:
    start = items[0]
    end = items[-1]
    start_seconds = _clock_seconds(start.decoder_clock)
    end_seconds = _clock_seconds(end.decoder_clock)
    duration: float | None = None
    timing_confidence = "line_order_only"
    if start_seconds is not None and end_seconds is not None:
        delta = end_seconds - start_seconds
        if delta < 0:
            delta += 24 * 3600
        if delta <= 3600:
            duration = float(delta)
            timing_confidence = "decoder_clock_estimate"
    colors = [item.color_code for item in items if item.color_code is not None]
    dominant = Counter(colors).most_common(1)[0][0] if colors else None
    return EventSession(
        session_index=index,
        slot=start.slot,
        session_type=_session_type(items),
        start_line=start.line_index,
        end_line=end.line_index,
        start_clock=start.decoder_clock,
        end_clock=end.decoder_clock,
        duration_seconds_estimate=duration,
        timing_confidence=timing_confidence,
        event_count=len(items),
        error_count=sum(item.is_error for item in items),
        dominant_color_code=dominant,
        talkgroup_ids=sorted(
            {
                item.talkgroup_id
                for item in items
                if item.talkgroup_id is not None
            }
        ),
        radio_ids=sorted(
            {item.radio_id for item in items if item.radio_id is not None}
        ),
        event_subtypes=sorted(
            {item.event_subtype for item in items if item.event_subtype}
        ),
    )


def correlate_sessions(
    events: Iterable[ParsedEvent],
    *,
    max_gap_lines: int = 12,
    identity_lookback_lines: int = 6,
    include_idle: bool = False,
) -> list[EventSession]:
    ordered = sorted(events, key=lambda item: (item.line_index, item.slot or 0))
    by_slot: dict[int | None, list[list[ParsedEvent]]] = {}
    most_recent: dict[int | None, list[ParsedEvent]] = {}

    for event in ordered:
        if event.event_type == "identity":
            candidates = [
                group
                for group in most_recent.values()
                if group
                and event.line_index - group[-1].line_index
                <= identity_lookback_lines
            ]
            if candidates:
                target = max(candidates, key=lambda group: group[-1].line_index)
                target.append(event)
            continue
        if event.event_type == "idle" and not include_idle:
            most_recent.pop(event.slot, None)
            continue
        slot_groups = by_slot.setdefault(event.slot, [])
        active = most_recent.get(event.slot)
        if active and event.line_index - active[-1].line_index <= max_gap_lines:
            active.append(event)
        else:
            active = [event]
            slot_groups.append(active)
            most_recent[event.slot] = active

    sessions: list[EventSession] = []
    session_index = 1
    for slot in sorted(by_slot, key=lambda value: -1 if value is None else value):
        for group in by_slot[slot]:
            sessions.append(_build_session(session_index, group))
            session_index += 1
    sessions.sort(key=lambda item: (item.start_line, item.slot or 0))
    for index, session in enumerate(sessions, start=1):
        session.session_index = index
    return sessions
