# DMR IQ Surveyor — next-conversation handoff

Read this document before continuing development. It is the authoritative handoff from the ChatGPT development session of 13 July 2026.

## 1. Repository and current state

Repository:

```text
https://github.com/Shahar373/dmr-iq-surveyor
```

The project began from a clean repository and now implements Phases 1–5 of an offline, receive-only DMR survey pipeline.

Current release:

```text
0.6.0
```

Do not restart the project or replace the established pipeline without evidence. Continue from the existing commands, data formats, tests and documentation.

All future field instructions must be written into the repository, not left only in chat messages.

Primary documents:

- `docs/development-history.md`
- `docs/phase4-design.md`
- `docs/phase5-design.md`
- `docs/FIELD-RECORDING-GUIDE.md`
- `docs/TRANSMITTER-LOCATION-STUDY.md`
- `docs/FIELD-SESSION-METADATA-TEMPLATE.csv`
- this handoff document

## 2. User and hardware context

User: Shahar Moshayof.

Primary execution platform:

- Raspberry Pi 5;
- Raspberry Pi OS / Debian-family environment;
- SDRplay RSP1B-class receiver;
- DSD-FME installed at `/usr/local/bin/dsd-fme` in the validated run;
- portable use is sometimes powered from a battery bank, so CPU, memory, I/O and undervoltage risk matter.

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

- an event ledger;
- per-slot correlated sessions/bursts;
- a persistent SQLite database;
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
| 164.537500 MHz | 8 | idle and Group Voice | complete VC1–VC6 evidence |
| 164.725000 MHz | 7 | idle/data | clean in both |
| 165.625000 MHz | 6 | idle/data | degraded despite strong RF |
| 167.137500 MHz | 7 | idle/data | one degraded, one clean |

Do not claim TG or Radio IDs for the original recordings. None were recovered reliably.

## 6. Does Phase 5 require another recording?

No.

Phase 5 can be run and validated entirely from the existing Phase 4.1 output tree. A new field recording is required only to add new evidence, including:

- Talkgroup and Radio IDs;
- longer voice/control activity;
- strength measurements from new locations;
- transmitter coverage/location work;
- cleaner evidence for degraded or passband-edge channels.

## 7. Reproducible commands on the Pi

```bash
cd ~/Projects/dmr-iq-surveyor
git checkout main
git pull --ff-only origin main
source .venv/bin/activate
python -m pip install -e '.[dev]'
pytest -q
ruff check .
```

Archived workflow:

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

## 8. Phase 5 outputs

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

## 9. Important interpretation rules

- DSD-FME clock strings are decoder-run evidence, not guaranteed original RF capture timestamps.
- Session durations are estimates only when decoder clock values are monotonic.
- DSD-FME `-xr` symbol inversion is not the same as IQ/QI orientation.
- PSD is relative dBFS/Hz, not calibrated dBm.
- A strong carrier may decode poorly because of overload, distortion, offset or transient timing.
- Empty TG/Radio lists must remain empty; never infer IDs from payload bytes without a documented protocol decoder.
- Color Code is not a unique transmitter or site identifier.

## 10. Future field recording protocol

The repository guide is authoritative:

```text
docs/FIELD-RECORDING-GUIDE.md
```

For the first multi-location campaign, the documented starting profile is:

```text
center frequency: 164.831250 MHz
sample rate:      10.000 MS/s
capture length:   15–20 seconds
repeats:          2 per site
sites:            8–12
AGC:              off
manual gain:      fixed for the campaign
```

One capture covers all eight confirmed channels simultaneously.

Every campaign must use:

```text
docs/FIELD-SESSION-METADATA-TEMPLATE.csv
```

Do not process long recordings in the field while the Pi is on unstable power. Capture first and analyze later.

## 11. Transmitter-location study

The repository methodology is:

```text
docs/TRANSMITTER-LOCATION-STUDY.md
```

Key conclusion:

- sequential recordings from one portable receiver can build a coverage heatmap and reduce the likely search area;
- RSSI alone normally cannot provide a precise transmitter coordinate;
- directional bearings from at least three sites are the recommended refinement;
- precise TDOA requires simultaneous time-synchronized receivers;
- coherent AoA requires phase-coherent multi-channel hardware, which a single RSP1B is not.

Recommended progression:

1. 8–12-site wideband survey;
2. closer 200–500 m refinement around the strongest region;
3. directional bearings from at least three sites;
4. TDOA or coherent AoA only when justified.

## 12. Open engineering work

### Issue #13 — Phase 5.1

Add adaptive extraction presets for:

- 10 MS/s wideband input;
- 500 kS/s targeted input;
- 250 kS/s targeted input.

Also add:

- known-frequency targeted processing without Phase 3;
- capture metadata persistence;
- standalone DSD-FME log import;
- validation tests and documentation.

Important: do not reuse the current 10 MS/s decimation/filter profile unchanged on a 250 or 500 kS/s file.

First targeted channel after Phase 5.1:

```text
164.537500 MHz
Color Code 8
```

## 13. Git workflow expectations

For each change:

1. create an issue with acceptance criteria;
2. create a feature branch from `main`;
3. implement code, tests and docs;
4. open a PR;
5. wait for GitHub Actions pytest and Ruff;
6. merge only after CI succeeds;
7. update this handoff when state changes materially.

## 14. Prompt for the next chat

```text
Continue the DMR IQ Surveyor project in https://github.com/Shahar373/dmr-iq-surveyor.

Read first:
- docs/NEXT-CONVERSATION-HANDOFF.md
- docs/development-history.md
- docs/FIELD-RECORDING-GUIDE.md
- docs/TRANSMITTER-LOCATION-STUDY.md
- GitHub Issue #13

Do not restart Phases 1–5.

First help me run and validate Phase 5 on the Raspberry Pi. Then implement Phase 5.1 from Issue #13 so targeted 250/500 kS/s captures are safe and supported.

After that, help me plan and analyze a multi-location recording campaign for all eight confirmed channels, using the repository field guide. Treat RSSI results as a coverage/probability study, not an exact transmitter coordinate.

Use issue -> branch -> implementation/tests/docs -> PR -> CI -> merge.
Keep the project passive and receive-only.
```

## 15. Safety and scope

This project is passive receive-side RF analysis. Do not add transmit, injection, impersonation, authentication bypass, brute force or decryption features.
