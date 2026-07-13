# DMR IQ Surveyor — next-conversation handoff

Read this document before continuing development. It is the authoritative handoff from the ChatGPT development session of 13 July 2026.

## 1. Repository and current state

Repository:

```text
https://github.com/Shahar373/dmr-iq-surveyor
```

Current release after Phase 5.1:

```text
0.7.0
```

The repository implements Phases 1–5.1 of an offline, passive, receive-only DMR survey pipeline. Do not restart the project or replace established commands, formats or tests without evidence.

All future field instructions must be committed to the repository rather than left only in chat.

Primary documents:

- `README.md`
- `docs/development-history.md`
- `docs/phase4-design.md`
- `docs/phase5-design.md`
- `docs/phase5-session-semantics.md`
- `docs/PHASE5-1-TARGETED-CAPTURE.md`
- `docs/FIELD-RECORDING-GUIDE.md`
- `docs/TRANSMITTER-LOCATION-STUDY.md`
- `docs/FIELD-SESSION-METADATA-TEMPLATE.csv`
- this handoff

## 2. User and hardware context

User: Shahar Moshayof.

Primary platform:

- Raspberry Pi 5;
- Raspberry Pi OS / Debian-family system;
- SDRplay RSP1B-class receiver;
- DSD-FME installed at `/usr/local/bin/dsd-fme` during validated runs;
- portable power can be unstable, so undervoltage, CPU, memory and I/O matter.

Repository on the Pi:

```text
/home/shahar/Projects/dmr-iq-surveyor
```

## 3. Development source recordings

```text
/home/shahar/Documents/SDRconnect_IQ_20260713_150242_163671500HZ.wav
/home/shahar/Documents/SDRconnect_IQ_20260713_150256_163671500HZ.wav
```

Properties:

- center: 163.671500 MHz;
- rate: 10,000,000 complex samples/s;
- signed 16-bit stereo IQ;
- durations: 6.800109 s and 11.200179 s;
- nominal coverage: 158.671500–168.671500 MHz;
- separate captures with an approximately 7.2-second gap; never concatenate them into a false timeline.

IQ/QI orientation remains assumed `IQ`, not independently proven.

## 4. Implemented phases

### Phase 1 — inspection

- RIFF/RF64 and SDRplay metadata parsing;
- filename center-frequency fallback;
- memory-mapped IQ reader;
- bounded integrity, clipping and statistics checks.

Known source quirk: declared data length exceeded available data by 36 bytes, treated as an SDRconnect RIFF/JUNK accounting issue rather than missing samples.

### Phase 2 — spectrum

- 65,536-point Hann FFT, 50% overlap;
- average, max-hold and deterministic P95;
- local noise floor, occupancy and reduced waterfall;
- Raspberry Pi runtime approximately 28 s and 47 s for the two source files.

Higher-confidence flat passband: approximately 159.49–167.68 MHz.

### Phase 3 — candidate detection

Detected:

```text
22 dmr_like_narrowband
2 wideband_unknown
4 narrow_carrier_or_spur
```

Every candidate preserves assumed-IQ and mirrored-QI frequencies.

### Phase 4 — extraction and DSD-FME

Pipeline:

```text
complex IQ
  -> phase-continuous mixer
  -> validated FIR decimation profile
  -> 50 or 100 kHz complex baseband
  -> 7.5 kHz channel filter
  -> FM discriminator
  -> 48 kHz mono PCM16
  -> DSD-FME normal/inverted attempts
```

### Phase 4.1 — evidence quality

- only signed `Sync: +DMR` / `Sync: -DMR` lines count;
- active slot comes from the bracketed slot token;
- polarity is selected by evidence quality, not raw sync count;
- statuses: `dmr_sync_only`, `dmr_confirmed_degraded`, `dmr_confirmed_clean`;
- peak-safe normalization produces zero clipped PCM16 samples by default.

Validated result:

```text
15 attempts
11 clean
4 degraded
normal selected 15/15
```

### Phase 5 — persistent inventory

- event ledger;
- per-slot correlated sessions;
- SQLite tables: `runs`, `attempts`, `events`, `sessions`, `channels`;
- idempotent replacement by `run_id`;
- accumulation across different run IDs;
- CSV, JSON, JSONL and Markdown exports.

Validated archived baseline:

```text
15 attempts
8 channels
4,811 events
146 total correlated sessions
45 meaningful sessions
101 error-only quality sessions
voice evidence on 164.537500 MHz only
no Talkgroup IDs
no Radio IDs
```

`error_only` sessions remain in SQLite and exports but are not described as calls or operational traffic.

### Phase 5.1 — targeted known-frequency workflow

Supported exact-rate extraction profiles:

| Profile | Required input rate | Intermediate rate |
|---|---:|---:|
| `10m` | 10,000,000 S/s | 100,000 S/s |
| `500k` | 500,000 S/s | 100,000 S/s |
| `250k` | 250,000 S/s | 50,000 S/s |
| `auto` | exact detected match | profile-dependent |

A mismatch fails before IQ processing.

Phase 5.1 adds:

- `targeted-decode` for a known frequency without Phase 2/3;
- capture metadata YAML/JSON validation and persistence;
- metadata stored in extraction reports, exports and `attempts.capture_metadata_json`;
- direct `inventory-import-log` for standalone DSD-FME logs;
- automatic migration of existing SQLite databases;
- helper `scripts/run_targeted_164537500.sh`;
- 30-minute targeted decoder timeout by default.

## 5. Confirmed DMR inventory

| Frequency | CC | Activity | Notes |
|---:|---:|---|---|
| 162.525000 MHz | 8 | CSBK/data | one attempt |
| 162.587500 MHz | 5 | CSBK/data | clean |
| 164.300000 MHz | 7 | mostly idle | clean |
| 164.325000 MHz | 6 | mostly idle | one degraded |
| 164.537500 MHz | 8 | idle + Group Voice | complete VC1–VC6 evidence |
| 164.725000 MHz | 7 | idle/data | clean |
| 165.625000 MHz | 6 | idle/data | degraded despite strong RF |
| 167.137500 MHz | 7 | idle/data | one degraded |

Do not claim TG or Radio IDs for the original short recordings; none were recovered reliably.

## 6. Commands on the Pi

Update and validate:

```bash
cd ~/Projects/dmr-iq-surveyor
git checkout main
git pull --ff-only origin main
source .venv/bin/activate
python -m pip install -e '.[dev]'
pytest -q
ruff check .
dmr-surveyor --help
```

Expected version:

```text
0.7.0
```

### Rebuild the archived Phase 5 inventory

```bash
./scripts/run_shahar_inventory.sh
cat runs/20260713_163671500Hz/inventory/phase5_report.md
```

Expected corrected session counts:

```text
total:      146
meaningful: 45
error-only: 101
```

### Process a targeted recording

```bash
cp config/targeted_capture_metadata.example.yaml \
  config/my_targeted_capture.yaml

chmod +x scripts/run_targeted_164537500.sh
./scripts/run_targeted_164537500.sh \
  /full/path/to/recording.wav \
  config/my_targeted_capture.yaml \
  field_YYYYMMDD_site_a \
  auto
```

Shared persistent database:

```text
runs/inventory/dmr_inventory.sqlite3
```

## 7. Next targeted field recording

Primary objective: recover explicit Talkgroup/Target and Radio/Source IDs.

First channel:

```text
164.537500 MHz
Color Code 8
```

Recommended first real profile:

```text
center frequency: 164.537500 MHz
sample rate:      500 kS/s
format:           signed int16 complex IQ WAV
capture length:   5–15 minutes
AGC:              off
manual gain:      fixed and recorded
antenna:          vertical VHF, repeatable geometry
```

Use 500 kS/s first for extra tuning/filter margin. `250k` is supported after the 500k path is validated on real data.

Before recording:

```bash
vcgencmd get_throttled
free -h
df -h /path/to/storage
timedatectl status
```

Desired power state:

```text
throttled=0x0
```

Capture first; process later on stable power.

Fill metadata based on:

```text
config/targeted_capture_metadata.example.yaml
```

## 8. Multi-location transmitter study

Separate objective from the targeted identity capture.

First survey profile:

```text
center frequency: 164.831250 MHz
sample rate:      10 MS/s
capture length:   15–20 seconds
repeats:          2 per site
sites:            8–12
AGC:              off
manual gain:      identical across the campaign
```

This records all eight confirmed channels simultaneously.

Interpretation:

- sequential one-receiver measurements support coverage heatmaps and a coarse probable region;
- RSSI alone does not justify an exact coordinate;
- refine with 200–500 m site spacing;
- then use directional bearings from at least three sites;
- precise TDOA requires simultaneous synchronized receivers;
- coherent AoA requires phase-coherent multi-channel hardware.

Authoritative guides:

```text
docs/FIELD-RECORDING-GUIDE.md
docs/TRANSMITTER-LOCATION-STUDY.md
```

## 9. Important interpretation rules

- DSD-FME clock strings are decoder-run evidence, not guaranteed RF source timestamps.
- Session duration is an estimate only when decoder clocks are monotonic.
- DSD-FME `-xr` is symbol inversion, not IQ/QI orientation.
- PSD is relative dBFS/Hz, not calibrated dBm.
- Color Code is not a unique transmitter/site identifier.
- Empty TG/Radio lists remain empty; never infer IDs from undocumented payload bytes.
- A strong carrier can decode poorly due to overload, distortion, offset or capture timing.

## 10. Recommended next work after the first targeted run

1. upload the targeted result archive;
2. verify profile, duration, zero clipping, stable CC8 and polarity;
3. inspect meaningful voice/control sessions;
4. confirm any TG/Radio IDs only from explicit parser evidence;
5. import the run with a unique `run_id` into the shared database;
6. compare against the archived inventory;
7. only then decide whether to repeat at 250 kS/s or another location/time;
8. later add protocol-aware call start/end boundaries and optional HTML inventory views.

## 11. Git workflow

For changes:

```text
issue -> feature branch -> implementation/tests/docs -> PR -> CI -> merge
```

Merge only after full pytest and Ruff succeed. Update this handoff whenever the project state changes materially.

## 12. Prompt for the next chat

```text
Continue the DMR IQ Surveyor project in:
https://github.com/Shahar373/dmr-iq-surveyor

Read first:
- docs/NEXT-CONVERSATION-HANDOFF.md
- docs/development-history.md
- docs/PHASE5-1-TARGETED-CAPTURE.md
- docs/FIELD-RECORDING-GUIDE.md
- docs/TRANSMITTER-LOCATION-STUDY.md

The project is at version 0.7.0 and Phases 1–5.1 are complete. Do not restart or redesign them.

First help me verify my Raspberry Pi is updated and that the archived Phase 5 inventory now reports 146 total sessions, 45 meaningful sessions and 101 error-only sessions.

Then help me prepare or analyze a targeted 500 kS/s IQ recording centered on 164.537500 MHz / CC8, using targeted-decode and the persistent SQLite inventory. Confirm TG and Radio IDs only from explicit evidence.

Later, help me run the documented multi-location 10 MS/s survey for coverage and coarse transmitter-region estimation. Treat sequential RSSI as a probability/coverage study, not an exact coordinate.

Use issue -> branch -> tests/docs -> PR -> CI -> merge. Keep the project passive and receive-only.
```

## 13. Safety and scope

This project is passive receive-side RF analysis. Do not add transmit, injection, impersonation, authentication bypass, brute-force or decryption features.
