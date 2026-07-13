# Phase 5 design — persistent DMR event and channel inventory

Phase 5 sits above the Phase 4.1 decoder outputs. It does not demodulate RF again and it does not infer identities that are absent from DSD-FME evidence. Its job is to turn decoder logs into a persistent, queryable history that can grow across future field sessions.

## Inputs

A Phase 4/4.1 decode tree:

```text
decodes/CANDIDATE_ID/RECORDING_ID/iq/
├── extraction_report.json
└── decoder/
    ├── decoder_report.json
    ├── dsd_fme_normal_stderr.log
    ├── dsd_fme_normal_stdout.log
    ├── dsd_fme_inverted_stderr.log
    └── dsd_fme_inverted_stdout.log
```

Phase 5 reads only the polarity selected by `decoder_report.json`.

## Event parser

The parser strips ANSI terminal formatting and preserves raw evidence plus source line order. It recognizes:

- signed `Sync: +DMR` and `Sync: -DMR` lines;
- the bracketed active slot, `[SLOT1]` or `[slot2]`;
- numeric Color Codes;
- IDLE, CSBK, DATA, VOICE, and VC1–VC6 stages;
- `Activity Update TS1/TS2` states;
- Talkgroup, Target, Source, and Radio IDs when explicitly printed;
- Motorola data-channel lines;
- logical-slot/network-state lines;
- CRC, FEC, CACH, frame, and other decoder-error evidence.

The event ledger stores decoder clock text, but it does not claim that the clock is the original RF capture time. Offline DSD-FME runs may print processing-time wall clocks.

## Session correlation

Non-idle events are grouped independently per slot. A new burst/session starts when the configurable line gap is exceeded or an IDLE event closes the active slot group. Identity lines are attached to the nearest recent active session within a bounded lookback.

Session timing fields are conservative:

- `line_order_only` when no usable decoder-clock range exists;
- `decoder_clock_estimate` when the decoder clock is monotonic and the interval is reasonable.

These records are useful for call/burst correlation, but they are not a substitute for source capture timestamps.

## Persistent database

Phase 5 uses the Python standard-library `sqlite3` module. Tables:

```text
runs
attempts
events
sessions
channels
```

The selected `run_id` is replaced atomically when imported again, making the command idempotent. Different run IDs remain in the same database and rebuild cumulative channel aggregates.

The `channels` table aggregates:

- frequency;
- dominant Color Code and consistency;
- clean, degraded, and sync-only attempt counts;
- slot activity;
- voice, data, control, and error counts;
- unioned Talkgroup and Radio IDs;
- first and last run IDs;
- best quality score and worst error ratio.

## Commands

Direct directory import:

```bash
dmr-surveyor inventory-build \
  runs/20260713_163671500Hz/decodes \
  --output runs/20260713_163671500Hz/inventory \
  --database runs/inventory/dmr_inventory.sqlite3 \
  --run-id 20260713_163671500Hz_phase4_1
```

Configured import:

```bash
dmr-surveyor inventory-batch config/shahar_recordings.yaml
```

Raspberry Pi helper:

```bash
chmod +x scripts/run_shahar_inventory.sh
./scripts/run_shahar_inventory.sh
```

## Outputs

```text
inventory/
├── attempts.csv
├── attempts.json
├── events.csv
├── events.json
├── events.jsonl
├── sessions.csv
├── sessions.json
├── channels.csv
├── channels.json
├── import_manifest.json
└── phase5_report.md

runs/inventory/
└── dmr_inventory.sqlite3
```

The per-run export directory is reproducible. The shared SQLite database is the long-lived inventory.

## Real-data regression baseline

The archived Phase 4.1 results produce:

```text
15 attempts
8 channels
4,811 parsed events
146 correlated non-idle sessions with max_gap_lines=12
voice evidence on 164.537500 MHz only
no Talkgroup IDs
no Radio IDs
```

The absence of IDs is preserved as an empty result and is never replaced with guesses.

## Field-collection strategy after Phase 5

Long 10 MHz IQ recordings should not be the default approach for collecting Talkgroup and Radio IDs. Signed 16-bit stereo IQ at 10 MS/s is approximately 40 MB/s, or about 2.4 GB per minute.

For identity collection:

1. select one confirmed DMR channel;
2. center the receiver on that channel rather than leaving it near a wideband edge;
3. record a narrower 250–500 kS/s IQ stream when the application permits;
4. capture 5–15 minutes during known operational activity;
5. preserve location, antenna, gain, center frequency, sample rate, and start time;
6. run Phase 4/4.1, then import the result with a new Phase 5 `run_id`.

At 250 kS/s, signed 16-bit complex IQ is roughly 60 MB per minute. At 500 kS/s, it is roughly 120 MB per minute. These captures are much more practical on the Raspberry Pi than 10 MHz wideband recordings.

## Passive scope

Phase 5 is receive-side log analysis and inventory management. It contains no transmit, injection, impersonation, authentication bypass, brute force, or decryption capability.
