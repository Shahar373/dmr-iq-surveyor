# DMR IQ Surveyor — next-conversation handoff

Read this document before continuing development. It is the authoritative handoff from the ChatGPT development session of 13 July 2026.

## 1. Repository and current state

Repository:

```text
https://github.com/Shahar373/dmr-iq-surveyor
```

The project began from a clean repository and now implements Phases 1–5 of an offline, receive-only DMR survey pipeline.

Current release after Phase 5:

```text
0.6.0
```

Do not restart the project or replace the established pipeline without evidence. Continue from the existing commands, data formats, tests, and documentation.

## 2. User and hardware context

User: Shahar Moshayof.

Primary execution platform:

- Raspberry Pi 5;
- Raspberry Pi OS / Debian-family environment;
- SDRplay RSP1B-class receiver;
- DSD-FME installed at `/usr/local/bin/dsd-fme` in the validated run;
- portable use is sometimes powered from a battery bank, so CPU, memory, I/O, and undervoltage risk matter.

Repository on the Pi:

```text
/home/shahar/Projects/dmr-iq-surveyor
```

## 3. Source recordings used for development

```text
/home/shahar/Documents/SDRconnect_IQ_20260713_150242_163671500HZ.wav
/home/shahar/Documents/SDRconnect_IQ_20260713_150256_163671500HZ.wav
```

Validated properties:

- center frequency: 163.671500 MHz;
- sample rate: 10,000,000 complex samples/s;
- signed 16-bit stereo IQ;
- durations: 6.800109 s and 11.200179 s;
- nominal coverage: 158.671500–168.671500 MHz;
- the files are separate captures with an approximately 7.2-second gap and must never be concatenated into a false continuous timeline.

## 4. Implemented phases

### Phase 1 — inspection

- RIFF/RF64 and SDRplay metadata parsing;
- filename center-frequency fallback;
- memory-mapped IQ reader;
- bounded statistics, clipping and integrity checks;
- batch consistency reports.

Important findings:

- center frequency came from the filename;
- a 36-byte declared/available data-length mismatch was treated as an SDRconnect RIFF/JUNK accounting quirk;
- no meaningful clipping or zero-filled regions;
- IQ/QI orientation remains an assumption.

### Phase 2 — spectrum

- 65,536-point Hann FFT;
- 50% overlap;
- approximately 152.588 Hz resolution;
- average, max-hold and P95 spectra;
- local noise floor, occupancy and reduced waterfall;
- bounded Raspberry Pi processing.

Observed runtime:

| Recording | FFTs | Runtime | Peak RSS |
|---|---:|---:|---:|
| 20260713_150242 | 2,074 | 28.06 s | ~741 MB |
| 20260713_150256 | 3,417 | 46.51 s | ~969 MB |

Higher-confidence flat passband was approximately 159.49–167.68 MHz.

### Phase 3 — candidate detection

The detector retained:

- 22 `dmr_like_narrowband` candidates;
- 2 `wideband_unknown` signals;
- 4 `narrow_carrier_or_spur` signals.

It uses integrated SNR, occupancy, occupied width, equivalent width, fill, symmetry, persistence, peak concentration, raster proximity and warnings.

### Phase 4 — channel extraction and DSD-FME

DSP path:

```text
10 MHz IQ
  -> phase-continuous mixer
  -> two FIR decimation stages
  -> 100 kHz complex baseband
  -> 7.5 kHz channel low-pass
  -> FM discriminator
  -> 48 kHz mono PCM16
  -> DSD-FME normal and inverted attempts
```

The source remains memory mapped. The Pi helper processes attempts sequentially with one numerical-library thread and low CPU/I/O priority.

### Phase 4.1 — quality corrections

The first real run exposed three bugs that are already fixed:

1. noisy inverted decoding was selected by raw sync count;
2. both slots were counted because DSD-FME prints both slot labels;
3. percentile normalization allowed PCM16 outliers to clip.

Current behavior:

- polarity is selected by an evidence-quality score;
- only signed `Sync: +DMR` or `Sync: -DMR` lines count;
- only the bracketed active slot is counted;
- statuses are `dmr_sync_only`, `dmr_confirmed_degraded`, or `dmr_confirmed_clean`;
- output WAV files are peak-safe and have zero clipped samples by default.

The corrected real run produced 15 attempts:

- 11 clean;
- 4 degraded;
- normal polarity selected in all 15;
- no TG or Radio IDs in the short captures.

### Phase 5 — persistent inventory

Phase 5 imports the selected DSD-FME logs into:

- event ledger;
- per-slot correlated sessions/bursts;
- persistent SQLite database;
- cumulative per-frequency channel aggregates.

It is idempotent by `run_id`: re-importing the same run replaces that run, while new run IDs accumulate in the shared database.

Expected archived Phase 4.1 baseline:

```text
15 attempts
8 channels
4,811 parsed events
146 correlated non-idle sessions
voice evidence on 164.537500 MHz only
no Talkgroup IDs
no Radio IDs
```

## 5. Confirmed DMR inventory

| Frequency | Color Code | Activity | Quality notes |
|---:|---:|---|---|
| 162.525000 MHz | 8 | CSBK/data | one recording |
| 162.587500 MHz | 5 | CSBK/data | clean in both |
| 164.300000 MHz | 7 | mostly idle | clean in both |
| 164.325000 MHz | 6 | mostly idle | one degraded, one clean |
| 164.537500 MHz | 8 | idle and Group Voice | voice evidence with VC1–VC6 |
| 164.725000 MHz | 7 | idle/data | clean in both |
| 165.625000 MHz | 6 | idle/data | degraded despite strong RF |
| 167.137500 MHz | 7 | idle/data | one degraded, one clean |

Do not claim TG or Radio IDs for these recordings. None were recovered reliably.

## 6. Reproducible commands on the Pi

```bash
cd ~/Projects/dmr-iq-surveyor
git checkout main
git pull --ff-only origin main
source .venv/bin/activate
python -m pip install -e '.[dev]'
pytest -q
ruff check .
```

Full archived workflow:

```bash
./scripts/run_shahar_recordings.sh
./scripts/run_shahar_spectrum.sh
./scripts/run_shahar_detection.sh
./scripts/run_shahar_decode.sh
chmod +x scripts/run_shahar_inventory.sh
./scripts/run_shahar_inventory.sh
```

Phase 5 direct command:

```bash
dmr-surveyor inventory-build \
  runs/20260713_163671500Hz/decodes \
  --output runs/20260713_163671500Hz/inventory \
  --database runs/inventory/dmr_inventory.sqlite3 \
  --run-id 20260713_163671500Hz_phase4_1
```

Configured command:

```bash
dmr-surveyor inventory-batch config/shahar_recordings.yaml
```

## 7. Phase 5 outputs

Per-run export:

```text
runs/20260713_163671500Hz/inventory/
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

Persistent database:

```text
runs/inventory/dmr_inventory.sqlite3
```

SQLite tables:

```text
runs
attempts
events
sessions
channels
```

## 8. Important interpretation rules

- DSD-FME clock strings are decoder-run evidence, not guaranteed original RF capture timestamps.
- Session durations are estimates only when decoder clock values are monotonic.
- DSD-FME `-xr` symbol inversion is not the same as IQ/QI orientation.
- PSD is relative dBFS/Hz, not calibrated dBm.
- A strong carrier may decode poorly because of overload, distortion, offset or transient timing.
- Candidates near the original 10 MHz passband edge should be re-recorded nearer the receiver center.
- Empty TG/Radio lists must remain empty; never infer IDs from payload bytes without a documented protocol decoder.

## 9. Recommended next field collection

The next objective is to capture real voice/control headers long enough to recover Talkgroup and Radio IDs.

Do not make long 10 MHz IQ recordings the default. At signed 16-bit complex IQ, 10 MS/s is approximately 40 MB/s or 2.4 GB/minute.

Recommended targeted workflow:

1. choose one confirmed frequency, starting with 164.537500 MHz because voice was already observed;
2. center the receiver exactly on that channel;
3. use 250–500 kS/s IQ when SDRconnect permits;
4. record 5–15 minutes during an active operational period;
5. record metadata: date/time, location, antenna, gain, center frequency, sample rate and power condition;
6. create a new config and output root for the capture;
7. run Phase 1, Phase 2 if useful, Phase 4/4.1 and then Phase 5 with a new run ID;
8. inspect `channels.json`, `sessions.json` and the SQLite database for new TG/Radio IDs.

Approximate storage:

- 250 kS/s complex int16: ~60 MB/minute;
- 500 kS/s complex int16: ~120 MB/minute;
- 10 MS/s complex int16: ~2.4 GB/minute.

For a battery-powered Pi, check before a long run:

```bash
vcgencmd get_throttled
free -h
df -h .
```

Prefer stable USB-C PD power or a UPS HAT. Do not run long RF processing while undervoltage flags are active.

## 10. Best next engineering work

After the first targeted longer capture, the recommended Phase 5.1 work is:

1. validate Phase 5 outputs against the new run;
2. add direct import of standalone DSD-FME logs that were not produced by Phase 4;
3. add capture metadata tables and real source timestamps;
4. improve session boundaries using protocol-aware call start/end messages;
5. add optional HTML dashboard views over the SQLite inventory;
6. verify IQ/QI orientation using a known off-center carrier;
7. consider a targeted live receiver mode only after the offline pipeline remains stable.

## 11. Git workflow expectations

For each change:

1. create an issue with acceptance criteria;
2. create a feature branch from `main`;
3. implement code, tests and docs;
4. open a PR;
5. wait for GitHub Actions pytest and Ruff;
6. merge only after CI succeeds;
7. update this handoff when state changes materially.

## 12. Prompt for the next chat

Use this as the opening prompt in a new conversation:

```text
Continue the DMR IQ Surveyor project in https://github.com/Shahar373/dmr-iq-surveyor.
Read docs/NEXT-CONVERSATION-HANDOFF.md and docs/development-history.md first.
Do not restart Phases 1–5.
Help me run and validate Phase 5 on the Raspberry Pi, analyze the uploaded inventory results, and then plan the first targeted 164.537500 MHz capture for recovering Talkgroup and Radio IDs.
Use issue -> branch -> tests/docs -> PR -> CI -> merge.
```

## 13. Safety and scope

This project is passive receive-side RF analysis. Do not add transmit, injection, impersonation, authentication bypass, brute force or decryption features.
