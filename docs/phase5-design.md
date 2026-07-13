# Phase 5 design — persistent DMR event and channel inventory

Phase 5 sits above Phase 4.1 decoder outputs. It does not infer identities absent from DSD-FME evidence. Its job is to turn logs into a persistent, queryable history across field sessions.

Phase 5.1 extends the input paths with targeted known-frequency processing, capture metadata and standalone-log import.

## Inputs

Standard Phase 4/4.1 tree:

```text
decodes/CANDIDATE_ID/RECORDING_ID/iq/
├── extraction_report.json
└── decoder/
    ├── decoder_report.json
    ├── dsd_fme_normal_stderr.log
    └── dsd_fme_inverted_stderr.log
```

Targeted trees use `T<frequency>` candidate IDs. Standalone imports use `L<frequency>` IDs. Phase 5 reads only the polarity selected by `decoder_report.json`.

## Event parser

The parser strips ANSI formatting, preserves raw line order and recognizes:

- signed `Sync: +DMR` / `Sync: -DMR`;
- bracketed active slot;
- numeric Color Code;
- IDLE, CSBK, DATA, VOICE and VC1–VC6;
- Activity Update states;
- explicit Talkgroup/Target and Radio/Source IDs;
- vendor/network-state evidence;
- CRC, FEC, CACH, frame and other decoder errors.

Decoder clock text is evidence only and is not treated as guaranteed RF capture time.

## Session correlation

Non-idle events are grouped independently per slot. A new group starts when the configurable line gap is exceeded or IDLE closes the slot. Identity lines attach to a nearby active group within a bounded lookback.

Session types:

```text
voice
data
control
mixed
idle
error_only
```

`error_only` means every event in the group is a decoder error. These groups remain in SQLite and exports for quality analysis but are excluded from `meaningful_sessions`.

Timing confidence:

- `line_order_only` when no usable clock range exists;
- `decoder_clock_estimate` when decoder clocks are monotonic and plausible.

## Persistent database

SQLite tables:

```text
runs
attempts
events
sessions
channels
```

Re-importing one `run_id` replaces that run before aggregates are rebuilt. Different run IDs accumulate.

The `attempts` table includes `capture_metadata_json`. Existing databases are migrated automatically when the column is missing.

The `channels` table aggregates:

- frequency and dominant CC consistency;
- clean/degraded/sync-only attempts;
- slot activity;
- voice/data/control/error counts;
- unioned explicit TG and Radio IDs;
- first/last run IDs;
- best quality and worst error ratio.

## Commands

Standard tree import:

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

Targeted known-frequency pipeline:

```bash
dmr-surveyor targeted-decode \
  /path/to/channel-centered.wav \
  --frequency 164537500 \
  --profile auto \
  --metadata config/my_targeted_capture.yaml \
  --run-id field_YYYYMMDD_site_a \
  --output runs/targeted/field_YYYYMMDD_site_a \
  --database runs/inventory/dmr_inventory.sqlite3
```

Standalone log import:

```bash
dmr-surveyor inventory-import-log \
  /path/to/dsd-fme.log \
  --frequency 164537500 \
  --run-id external_YYYYMMDD_site_a \
  --recording-id site_a \
  --metadata config/my_targeted_capture.yaml \
  --output runs/standalone/external_YYYYMMDD_site_a \
  --database runs/inventory/dmr_inventory.sqlite3
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
```

The report and manifest expose:

```text
sessions
meaningful_sessions
error_only_sessions
session_types
```

## Validated archived baseline

```text
15 attempts
8 channels
4,811 events
146 total sessions
45 meaningful sessions
101 error-only sessions
voice evidence only on 164.537500 MHz
no Talkgroup IDs
no Radio IDs
```

## Phase 5.1 extraction profiles

| Profile | Exact input rate | Complex intermediate rate |
|---|---:|---:|
| `10m` | 10,000,000 S/s | 100,000 S/s |
| `500k` | 500,000 S/s | 100,000 S/s |
| `250k` | 250,000 S/s | 50,000 S/s |
| `auto` | exact detected match | selected profile |

Profile-rate mismatches fail before IQ processing. All profiles end at 48 kHz mono PCM16 with peak-safe normalization.

Metadata can declare sample rate and center frequency; these values are validated against the recording before processing.

## Field strategy

For identity collection, start with:

```text
164.537500 MHz / CC8
500 kS/s
5–15 minutes
AGC off
fixed manual gain
```

500 kS/s is preferred for the first real targeted run because it provides more tuning and filtering margin. `250k` is supported after real-data validation.

For multi-location coverage work, retain the documented 10 MS/s, 15–20 second, two-repeat survey profile. See:

- `docs/PHASE5-1-TARGETED-CAPTURE.md`
- `docs/FIELD-RECORDING-GUIDE.md`
- `docs/TRANSMITTER-LOCATION-STUDY.md`

## Passive scope

Phase 5 and Phase 5.1 are receive-side processing and inventory management. They contain no transmit, injection, impersonation, authentication bypass, brute-force or decryption capability.
