# Phase 5 session semantics

Phase 5 preserves every parsed DSD-FME event, including decoder errors. Session counts must therefore distinguish RF communication activity from quality-only evidence.

## Session types

- `voice` — at least one voice or VC-stage event;
- `data` — data or vendor-data activity and no voice;
- `control` — control or network-state activity and no voice/data;
- `mixed` — meaningful event types that do not fit the categories above;
- `idle` — retained only when idle inclusion is explicitly enabled;
- `error_only` — every event in the correlated group is a decoder error.

A session containing both a real control/data/voice event and one or more decoder errors keeps its meaningful operational type. Only groups made entirely of errors receive `error_only`.

## Reporting contract

Phase 5 reports three counts:

```text
sessions               all retained correlated groups
meaningful_sessions    total excluding error_only
error_only_sessions    quality-only groups
```

Error-only sessions remain in CSV, JSON and SQLite so decoder quality can be analyzed later. They must not be described as calls, bursts or operational sessions.

## Archived baseline

For the 13 July 2026 Phase 4.1 result set:

```text
total sessions:      146
meaningful sessions: 45
error-only sessions: 101
```

The event count, channel inventory, Color Codes and voice findings remain unchanged by this classification correction.
