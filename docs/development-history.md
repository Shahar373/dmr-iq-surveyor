# DMR IQ Surveyor — development history

This is the chronological engineering record of the project built during the ChatGPT development session of 13 July 2026. It is not a verbatim transcript. It records the source data, architecture, implementation phases, validation, discovered bugs, Git workflow, known findings and recommended next work.

## 1. Objective

The repository began empty. The goal was to build a Raspberry Pi-friendly, offline receive-side pipeline for SDRconnect IQ recordings:

1. validate recording metadata and sample integrity;
2. generate reproducible wideband spectrum products;
3. detect and rank narrowband DMR-like candidates;
4. extract decoder-ready discriminator audio;
5. run DSD-FME and preserve raw evidence;
6. distinguish clean, degraded and false-looking decoder output;
7. accumulate frequencies, Color Codes, slots, voice/data activity, Talkgroups and Radio IDs across future sessions.

The project is passive and receive-only. It has no transmit, injection, impersonation, authentication bypass, brute-force or decryption capability.

## 2. Source recordings

```text
/home/shahar/Documents/SDRconnect_IQ_20260713_150242_163671500HZ.wav
/home/shahar/Documents/SDRconnect_IQ_20260713_150256_163671500HZ.wav
```

Validated properties:

| Property | Recording 1 | Recording 2 |
|---|---:|---:|
| Center frequency | 163.671500 MHz | 163.671500 MHz |
| Sample rate | 10,000,000 complex samples/s | 10,000,000 complex samples/s |
| Encoding | signed 16-bit stereo IQ | signed 16-bit stereo IQ |
| Duration | 6.800109 s | 11.200179 s |
| Nominal coverage | 158.671500–168.671500 MHz | 158.671500–168.671500 MHz |

The files are separate captures with an approximately 7.2-second gap. They are always analyzed independently and are never concatenated into a false continuous timeline.

## 3. Phase 1 — recording inspection

Phase 1 implemented:

- RIFF and RF64 parsing;
- `ds64`, `fmt ` and SDRplay-style `auxi` handling;
- center-frequency fallback from the SDRconnect filename;
- memory-mapped IQ reading;
- bounded statistics and diagnostic plots;
- clipping, zero-region and integrity checks;
- batch consistency reports.

Findings:

- center frequency was absent from the container metadata and was derived from `_163671500HZ`;
- declared data length exceeded available data by 36 bytes, consistent with an SDRconnect RIFF/JUNK accounting quirk;
- no meaningful clipping or zero-filled regions were found;
- I/Q balance was suitable for spectral work;
- IQ/QI orientation remained an assumption.

Git history:

- PR #2 completed the Phase 1 baseline and metadata edge-case fixes.

## 4. Phase 2 — streamed spectrum analysis

Phase 2 added:

- 65,536-point Hann FFTs;
- 50% overlap;
- approximately 152.588 Hz frequency resolution;
- average, max-hold and deterministic P95 spectra;
- adaptive local noise floor;
- per-frequency occupancy;
- reduced 500 × 8,192 waterfall products;
- independent batch processing and combined plots.

Raspberry Pi results:

| Recording | FFT count | Runtime | Peak RSS |
|---|---:|---:|---:|
| 20260713_150242 | 2,074 | 28.06 s | ~741 MB |
| 20260713_150256 | 3,417 | 46.51 s | ~969 MB |

The useful flat passband was narrower than the nominal 10 MHz. Approximately 159.49–167.68 MHz was treated as the higher-confidence region.

Git history:

- PR #4 added Phase 2 and passed GitHub Actions.

## 5. Phase 3 — candidate detection

Candidate scoring used multiple spectral features rather than a peak-only threshold:

- integrated average and P95 SNR;
- occupancy;
- 90% and equivalent occupied width;
- fill ratio and symmetry;
- peak concentration;
- persistence across recordings;
- 6.25 kHz and 12.5 kHz raster proximity;
- DC, edge and passband warnings.

The detector retained:

```text
22 dmr_like_narrowband
2 wideband_unknown
4 narrow_carrier_or_spur
```

The first decoding priorities were:

```text
165.625000 MHz
164.725000 MHz
164.300000 MHz
164.325000 MHz
162.587500 MHz
164.537500 MHz
167.137500 MHz
164.637500 MHz
```

Every candidate retained both the assumed-IQ frequency and the mirrored QI alternative.

Git history:

- PR #6 added Phase 3 and passed pytest and Ruff.

## 6. Phase 4 — narrowband extraction and DSD-FME

Receive-side DSP:

```text
10 MHz complex IQ
  -> phase-continuous mixer
  -> FIR anti-alias filtering
  -> decimate 10:1
  -> second FIR stage
  -> decimate 10:1 to 100 kHz complex baseband
  -> 7.5 kHz complex channel low-pass
  -> FM phase discriminator
  -> rational resampling to 48 kHz
  -> mono signed 16-bit PCM WAV
  -> DSD-FME normal and inverted attempts
```

The source remains memory mapped. Attempts run sequentially. The Raspberry Pi helper restricts numerical libraries to one thread and uses reduced CPU and I/O priority.

Phase 4 added:

- `extract-channel`;
- `decode-channel`;
- `decode-batch`;
- DSP and memory provenance;
- DSD-FME help fingerprinting;
- bounded timeouts;
- stdout/stderr capture;
- conservative Color Code, slot, Talkgroup and Radio ID parsing.

Git history:

- PR #8 added Phase 4 and passed CI.

## 7. First real decoder run and Phase 4.1

The first batch processed 15 candidate/recording attempts and produced real DMR evidence. It also exposed three bugs:

1. polarity was selected by raw sync-line count, so noisy inverted attempts beat coherent normal attempts;
2. both slots were counted because DSD-FME prints both labels on every status line;
3. percentile normalization allowed rare PCM16 outliers to clip.

The raw normal-polarity logs contained stable numeric Color Codes and coherent IDLE/CSBK/DATA/VOICE sequences. Many inverted lines contained `Color Code=XX`, repeated `VC1`, CRC/FEC/CACH errors and frame-sync errors.

Phase 4.1 replaced the binary confirmation rule with:

```text
dmr_sync_only
dmr_confirmed_degraded
dmr_confirmed_clean
```

The corrected scorer:

- strips ANSI formatting;
- counts only signed `Sync: +DMR` or `Sync: -DMR` lines;
- counts only the bracketed active slot;
- measures numeric Color Code ratio and dominant-CC consistency;
- records CRC, FEC, CACH, frame and voice errors;
- rewards coherent IDLE/CSBK/DATA and full voice-stage diversity;
- penalizes repetitive single-stage false patterns;
- chooses polarity by a documented quality score.

PCM normalization now selects the smaller of a percentile-derived scale and a hard peak-safe scale. The corrected WAV files contain zero clipped samples by default.

Real Phase 4.1 result:

```text
15 attempts
11 dmr_confirmed_clean
4 dmr_confirmed_degraded
0 dmr_sync_only
normal polarity selected in all 15
```

Git history:

- Issue #9 tracked the fixes.
- PR #10 implemented Phase 4.1, added the original handoff documentation and passed CI.

## 8. Validated short-capture DMR inventory

| Frequency | Dominant Color Code | Observed activity | Attempts |
|---:|---:|---|---:|
| 162.525000 MHz | 8 | CSBK/data | 1 |
| 162.587500 MHz | 5 | CSBK/data | 2 |
| 164.300000 MHz | 7 | mostly idle | 2 |
| 164.325000 MHz | 6 | mostly idle | 2 |
| 164.537500 MHz | 8 | idle and Group Voice | 2 |
| 164.725000 MHz | 7 | idle/data | 2 |
| 165.625000 MHz | 6 | idle/data, degraded | 2 |
| 167.137500 MHz | 7 | idle/data | 2 |

Complete `VC1` through `VC6` voice-stage activity was observed on 164.537500 MHz.

No reliable Talkgroup or Radio IDs were recovered. The captures were short and mostly contained idle, control or data traffic. Empty ID lists are evidence-preserving results and are not replaced with guesses.

## 9. Phase 5 — persistent event and channel inventory

Issue #11 defined Phase 5.

Phase 5 adds a layer above the Phase 4.1 outputs. It imports the selected best-polarity DSD-FME log and creates:

- a raw event ledger;
- per-slot correlated non-idle sessions/bursts;
- an idempotent per-run import;
- a persistent SQLite database;
- cumulative per-frequency channel aggregates;
- CSV, JSON, JSONL and Markdown exports.

Recognized evidence includes:

- signed DMR syncs;
- active slot and Color Code;
- IDLE, CSBK, DATA, VOICE and VC1–VC6;
- Activity Update states;
- explicit Talkgroup/Target and Radio/Source IDs;
- Motorola data-channel lines;
- network-state lines;
- decoder errors.

SQLite tables:

```text
runs
attempts
events
sessions
channels
```

Re-importing the same `run_id` deletes and replaces that run before rebuilding channel aggregates. A different run ID accumulates into the shared database.

The DSD-FME clock is stored as decoder-clock evidence only. It is not claimed to be the original RF capture timestamp.

Real Phase 5 regression against the archived Phase 4.1 output:

```text
15 attempts
8 channels
4,811 parsed events
146 correlated non-idle sessions with max_gap_lines=12
voice evidence on 164.537500 MHz only
no Talkgroup IDs
no Radio IDs
```

Version after Phase 5:

```text
0.6.0
```

## 10. Reproducible workflow

```bash
cd ~/Projects/dmr-iq-surveyor
source .venv/bin/activate

./scripts/run_shahar_recordings.sh
./scripts/run_shahar_spectrum.sh
./scripts/run_shahar_detection.sh
./scripts/run_shahar_decode.sh
./scripts/run_shahar_inventory.sh
```

Primary archived output root:

```text
runs/20260713_163671500Hz/
```

Persistent inventory database:

```text
runs/inventory/dmr_inventory.sqlite3
```

The source recordings and generated `runs/` tree remain outside Git.

## 11. Current limitations

- IQ versus QI frequency orientation is still unverified.
- PSD is relative dBFS/Hz, not calibrated dBm.
- The archived captures are too short for a reliable TG/Radio ID inventory.
- DSD-FME wall-clock text is not a guaranteed source capture timestamp.
- Session boundaries are line-gap correlations, not yet protocol-aware call start/end boundaries.
- Passband-edge candidates should be re-recorded nearer the receiver center.
- Strong RF can decode poorly because of overload, distortion, offset or transient timing.

## 12. Recommended next work

1. run Phase 5 on the Raspberry Pi and upload the inventory export;
2. validate the 15-attempt, 8-channel, 4,811-event baseline;
3. make a targeted 164.537500 MHz capture during active voice traffic;
4. prefer 250–500 kS/s for 5–15 minutes instead of a long 10 MHz capture;
5. preserve date/time, location, antenna, gain, center frequency, sample rate and power condition;
6. import each new session with a new Phase 5 `run_id`;
7. add direct import of standalone DSD-FME logs in Phase 5.1;
8. add capture metadata and trustworthy source timestamps;
9. make session boundaries protocol-aware;
10. verify IQ/QI orientation using a known off-center carrier.

See [`NEXT-CONVERSATION-HANDOFF.md`](NEXT-CONVERSATION-HANDOFF.md) for the exact next-chat prompt and operational handoff.
