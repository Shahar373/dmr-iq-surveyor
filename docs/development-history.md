# DMR IQ Surveyor — development history

This is the chronological engineering record of the project built during the ChatGPT development session of 13 July 2026. It is not a verbatim transcript. It records the data, architecture, implementation phases, validation, bugs, Git workflow, findings and current next steps.

## 1. Objective

The repository began empty. The goal was to build a Raspberry Pi-friendly, offline receive-side pipeline for SDRconnect IQ recordings:

1. validate recording metadata and sample integrity;
2. generate reproducible spectrum products;
3. detect and rank DMR-like narrowband channels;
4. extract decoder-ready discriminator audio;
5. run DSD-FME and preserve raw evidence;
6. distinguish clean, degraded and false-looking decoder output;
7. accumulate channels, Color Codes, slots, activity and explicit identities across future sessions;
8. support reproducible field captures and conservative transmitter-region studies.

The project is passive and receive-only. It contains no transmit, injection, impersonation, authentication bypass, brute-force or decryption capability.

## 2. Source recordings

```text
/home/shahar/Documents/SDRconnect_IQ_20260713_150242_163671500HZ.wav
/home/shahar/Documents/SDRconnect_IQ_20260713_150256_163671500HZ.wav
```

| Property | Recording 1 | Recording 2 |
|---|---:|---:|
| Center | 163.671500 MHz | 163.671500 MHz |
| Rate | 10,000,000 complex S/s | 10,000,000 complex S/s |
| Encoding | signed 16-bit stereo IQ | signed 16-bit stereo IQ |
| Duration | 6.800109 s | 11.200179 s |
| Nominal coverage | 158.671500–168.671500 MHz | 158.671500–168.671500 MHz |

The captures are independent and separated by about 7.2 seconds. They are never concatenated into a false continuous timeline.

## 3. Phase 1 — recording inspection

Implemented:

- RIFF/RF64, `ds64`, `fmt ` and SDRplay-style `auxi` handling;
- center-frequency fallback from SDRconnect filenames;
- memory-mapped IQ reading;
- bounded statistics and plots;
- clipping, zero-region and integrity checks;
- batch consistency reports.

Findings:

- center frequency came from `_163671500HZ`;
- declared data length exceeded available data by 36 bytes, consistent with a RIFF/JUNK accounting quirk;
- no meaningful clipping or zero-filled regions;
- IQ balance was adequate;
- IQ/QI orientation remained assumed, not proven.

Git: PR #2 completed Phase 1.

## 4. Phase 2 — streamed spectrum analysis

Implemented:

- 65,536-point Hann FFTs, 50% overlap;
- average, max-hold and deterministic P95 spectra;
- local noise floor and occupancy;
- reduced 500 × 8,192 waterfalls;
- independent batch processing.

Pi results:

| Recording | FFTs | Runtime | Peak RSS |
|---|---:|---:|---:|
| 20260713_150242 | 2,074 | 28.06 s | ~741 MB |
| 20260713_150256 | 3,417 | 46.51 s | ~969 MB |

Higher-confidence passband: approximately 159.49–167.68 MHz.

Git: PR #4 added Phase 2.

## 5. Phase 3 — candidate detection

Scoring combined:

- integrated average and P95 SNR;
- occupancy;
- occupied and equivalent widths;
- fill, symmetry and peak concentration;
- persistence across recordings;
- raster proximity;
- DC, edge and passband warnings.

Retained:

```text
22 dmr_like_narrowband
2 wideband_unknown
4 narrow_carrier_or_spur
```

Every candidate preserved assumed-IQ and mirrored-QI frequencies.

Git: PR #6 added Phase 3.

## 6. Phase 4 — narrowband extraction and DSD-FME

Original 10 MS/s DSP:

```text
complex IQ
  -> phase-continuous mixer
  -> FIR anti-alias filtering
  -> decimate 10:1
  -> second FIR and decimate 10:1
  -> 100 kHz complex baseband
  -> 7.5 kHz channel filter
  -> FM discriminator
  -> 48 kHz mono PCM16
  -> DSD-FME normal/inverted profiles
```

Added:

- `extract-channel`, `decode-channel`, `decode-batch`;
- streamed processing and bounded memory;
- DSD-FME probe/fingerprint, timeout and raw logs;
- conservative CC, slot, TG and Radio ID parsing.

Git: PR #8 added Phase 4.

## 7. Phase 4.1 — decoder quality correction

The first real batch exposed:

1. noisy inverted attempts winning by raw sync count;
2. both slots being counted because both labels appear on each DSD-FME line;
3. percentile normalization clipping rare PCM16 outliers.

Corrections:

- strip ANSI formatting;
- count only signed `Sync: +DMR` / `Sync: -DMR`;
- parse only the bracketed active slot;
- score numeric CC consistency, clean syncs, errors, coherent events and voice-stage diversity;
- penalize repeated false `VC1` patterns;
- use peak-safe PCM scaling.

Statuses:

```text
dmr_sync_only
dmr_confirmed_degraded
dmr_confirmed_clean
```

Validated result:

```text
15 attempts
11 clean
4 degraded
normal polarity selected 15/15
zero clipped output samples
```

Git: Issue #9 and PR #10.

## 8. Validated DMR inventory

| Frequency | CC | Activity | Attempts |
|---:|---:|---|---:|
| 162.525000 MHz | 8 | CSBK/data | 1 |
| 162.587500 MHz | 5 | CSBK/data | 2 |
| 164.300000 MHz | 7 | mostly idle | 2 |
| 164.325000 MHz | 6 | mostly idle | 2 |
| 164.537500 MHz | 8 | idle and Group Voice | 2 |
| 164.725000 MHz | 7 | idle/data | 2 |
| 165.625000 MHz | 6 | idle/data, degraded | 2 |
| 167.137500 MHz | 7 | idle/data | 2 |

Complete VC1–VC6 activity appeared at 164.537500 MHz. No reliable TG or Radio IDs appeared in the short captures.

## 9. Phase 5 — persistent inventory

Issue #11 and PR #12 added:

- selected best-polarity log import;
- raw event ledger;
- line-gap session correlation per slot;
- idempotent `run_id` replacement;
- persistent SQLite aggregation;
- CSV, JSON, JSONL and Markdown exports.

SQLite tables:

```text
runs
attempts
events
sessions
channels
```

Validated baseline:

```text
15 attempts
8 channels
4,811 events
146 correlated groups
voice evidence on 164.537500 MHz only
no TG IDs
no Radio IDs
```

Version: 0.6.0.

## 10. Phase 5.0.1 — session semantics

Validation showed that 101 of the 146 correlated groups contained only decoder errors. Treating them as `mixed` overstated operational activity.

Issue #16 and PR #17 added:

- `error_only` session type;
- total, meaningful and error-only report counts;
- retention of every error event and group for quality analysis;
- documentation and tests.

Corrected baseline:

```text
total sessions:      146
meaningful sessions: 45
error-only sessions: 101
```

Meaningful approximate breakdown:

```text
31 data
6 control
5 mixed
3 voice
```

Version: 0.6.1.

## 11. Field recording and transmitter-location documentation

Issue #14 and PR #15 added:

- `docs/FIELD-RECORDING-GUIDE.md`;
- `docs/TRANSMITTER-LOCATION-STUDY.md`;
- `docs/FIELD-SESSION-METADATA-TEMPLATE.csv`;
- repository-native field procedures.

Geolocation conclusion:

- one receiver moved sequentially can produce coverage maps and a coarse probable region;
- RSSI alone does not justify an exact coordinate;
- refine with closer sites and directional bearings;
- TDOA requires simultaneous synchronized receivers;
- coherent AoA requires coherent multi-channel hardware.

First wideband campaign profile:

```text
center:    164.831250 MHz
rate:      10 MS/s
duration:  15–20 s
repeats:   2 per site
sites:     8–12
AGC:       off
manual gain fixed
```

## 12. Phase 5.1 — targeted captures and metadata

Issue #13 introduced safe lower-rate targeted processing.

Supported exact profiles:

| Profile | Input | Intermediate |
|---|---:|---:|
| `10m` | 10,000,000 S/s | 100,000 S/s |
| `500k` | 500,000 S/s | 100,000 S/s |
| `250k` | 250,000 S/s | 50,000 S/s |
| `auto` | exact detected match | profile-dependent |

A mismatch fails before IQ processing.

Added:

- `targeted-decode`, bypassing Phase 2/3 for a known channel;
- `inventory-import-log` for external DSD-FME logs;
- metadata YAML/JSON validation against the recording;
- metadata persistence in extraction reports, exports and SQLite;
- migration of existing SQLite databases;
- `scripts/run_targeted_164537500.sh`;
- synthetic 250k/500k DSP tests;
- standalone-log and metadata tests;
- 30-minute targeted decoder timeout.

First targeted field profile:

```text
frequency/center: 164.537500 MHz
Color Code:       8
rate:             500 kS/s preferred
length:           5–15 minutes
AGC:              off
manual gain:      fixed and documented
```

Version: 0.7.0.

## 13. Reproducible commands

Archived workflow:

```bash
cd ~/Projects/dmr-iq-surveyor
source .venv/bin/activate
./scripts/run_shahar_recordings.sh
./scripts/run_shahar_spectrum.sh
./scripts/run_shahar_detection.sh
./scripts/run_shahar_decode.sh
./scripts/run_shahar_inventory.sh
```

Targeted workflow:

```bash
cp config/targeted_capture_metadata.example.yaml \
  config/my_targeted_capture.yaml
chmod +x scripts/run_targeted_164537500.sh
./scripts/run_targeted_164537500.sh \
  /path/to/recording.wav \
  config/my_targeted_capture.yaml \
  field_YYYYMMDD_site_a \
  auto
```

Persistent database:

```text
runs/inventory/dmr_inventory.sqlite3
```

## 14. Current limitations

- IQ/QI frequency orientation remains unverified.
- PSD is relative dBFS/Hz, not calibrated dBm.
- session boundaries are line-gap correlations rather than protocol-aware call boundaries;
- DSD-FME wall-clock text is not guaranteed source capture time;
- RSSI-only localization is coarse and propagation-dependent;
- sensitive infrastructure locations require careful validation and responsible handling.

## 15. Recommended next work

1. update the Pi to version 0.7.0 and run full tests;
2. rebuild the archived Phase 5 inventory and verify 146/45/101 session counts;
3. record 164.537500 MHz at 500 kS/s for 5–15 minutes during activity;
4. process it through `targeted-decode` with a unique run ID and metadata;
5. upload the targeted output archive;
6. validate CC8, polarity, clipping, sessions and explicit identities;
7. later conduct the documented multi-location 10 MS/s survey;
8. add protocol-aware call boundaries and optional HTML inventory views only after real targeted validation.

See `NEXT-CONVERSATION-HANDOFF.md` for the exact next-chat prompt.
