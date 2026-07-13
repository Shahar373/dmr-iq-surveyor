# DMR IQ Surveyor

Offline Python tooling for inspecting SDRconnect wideband IQ recordings, producing spectrum products, detecting DMR-like channels, extracting decoder-ready audio, running DSD-FME, and maintaining a persistent channel inventory.

The project is designed for a Raspberry Pi and SDRplay workflow. Wideband IQ files remain memory mapped and heavy stages run sequentially.

## Implemented stages

- Phase 1: RIFF/RF64 and SDRplay metadata inspection
- Phase 2: streamed FFT, noise floor, occupancy and waterfall
- Phase 3: narrowband candidate detection and ranking
- Phase 4: streamed channel extraction and DSD-FME attempts
- Phase 4.1: evidence-quality polarity scoring, active-slot parsing and peak-safe PCM
- Phase 5: persistent event, session and channel inventory in SQLite

Project records:

- [`docs/development-history.md`](docs/development-history.md)
- [`docs/phase4-design.md`](docs/phase4-design.md)
- [`docs/phase5-design.md`](docs/phase5-design.md)
- [`docs/NEXT-CONVERSATION-HANDOFF.md`](docs/NEXT-CONVERSATION-HANDOFF.md)

## Install

```bash
sudo apt update
sudo apt install -y git python3 python3-venv python3-pip
cd dmr-iq-surveyor
./scripts/bootstrap.sh
source .venv/bin/activate
```

DSD-FME is optional for extraction. When it is missing, Phase 4 still creates discriminator WAV files and records `decoder_unavailable`.

## Configured Shahar workflow

```bash
cd ~/Projects/dmr-iq-surveyor
source .venv/bin/activate

./scripts/run_shahar_recordings.sh
./scripts/run_shahar_spectrum.sh
./scripts/run_shahar_detection.sh
./scripts/run_shahar_decode.sh
chmod +x scripts/run_shahar_inventory.sh
./scripts/run_shahar_inventory.sh
```

The two original SDRconnect recordings are analyzed independently and are never concatenated across their gap.

## Phase 1 — inspection

```bash
dmr-surveyor inspect /path/to/recording.wav --output runs/my-run/inspect
dmr-surveyor inspect-batch config/shahar_recordings.yaml
```

Phase 1 validates container metadata, sample encoding, center frequency, frame counts, clipping, zero regions and bounded IQ statistics.

## Phase 2 — spectrum

```bash
dmr-surveyor spectrum \
  /path/to/recording.wav \
  --output runs/my-run/spectrum

dmr-surveyor spectrum-batch config/shahar_recordings.yaml
```

Spectrum artifacts include:

```text
spectrum/
├── average_spectrum.csv
├── average_spectrum.png
├── max_hold_spectrum.png
├── percentile_spectrum.csv
├── noise_floor.csv
├── occupancy.csv
├── waterfall.npy
├── waterfall_axes.npz
├── waterfall.png
├── spectrum_report.json
└── report.md
```

Power is relative `dBFS/Hz`, not calibrated dBm. DC and passband-edge regions are flagged rather than silently removed.

## Phase 3 — candidate detection

```bash
dmr-surveyor detect \
  runs/my-run/spectrum \
  --output runs/my-run/candidates

dmr-surveyor detect-batch config/shahar_recordings.yaml
```

The detector scores integrated average and P95 SNR, occupancy, occupied width, equivalent width, spectral fill, symmetry, persistence, raster proximity and peak concentration.

A `dmr_like_narrowband` label is a spectral hypothesis, not decoder confirmation.

Candidate artifacts:

```text
candidates/
├── candidates.csv
├── candidates.json
├── candidate_evidence.json
├── rejected_evidence.json
├── average_spectrum_annotated.png
├── waterfall_annotated.png
└── candidate_report.md
```

## Phase 4 — extraction and DSD-FME

Extract one channel:

```bash
dmr-surveyor extract-channel \
  /path/to/recording.wav \
  --frequency 165625000 \
  --output runs/manual-165625000
```

Decode an existing discriminator WAV:

```bash
dmr-surveyor decode-channel \
  runs/manual-165625000/discriminator.wav \
  --output runs/manual-165625000/decoder
```

Run ranked candidates:

```bash
dmr-surveyor decode-batch config/shahar_recordings.yaml
```

DSP path:

```text
wideband complex IQ
  -> phase-continuous mixer
  -> two FIR decimation stages
  -> 100 kHz complex baseband
  -> channel low-pass
  -> FM phase discriminator
  -> rational resampling
  -> 48 kHz mono PCM16
  -> DSD-FME normal and inverted profiles
```

Each attempt produces:

```text
decodes/CANDIDATE_ID/RECORDING_ID/iq/
├── discriminator.wav
├── extraction_report.json
├── extraction_report.md
├── baseband_preview.npz
└── decoder/
    ├── dsd_fme_normal_stdout.log
    ├── dsd_fme_normal_stderr.log
    ├── dsd_fme_inverted_stdout.log
    ├── dsd_fme_inverted_stderr.log
    ├── decoder_report_normal.json
    ├── decoder_report_inverted.json
    ├── decoder_report.json
    └── decoder_report.md
```

## Phase 4.1 — decoder evidence quality

Only signed `Sync: +DMR` or `Sync: -DMR` lines count as explicit sync evidence. Attempts are classified as:

```text
dmr_sync_only
dmr_confirmed_degraded
dmr_confirmed_clean
```

Polarity scoring considers:

- numeric Color Code ratio
- dominant Color Code consistency
- clean signed-sync ratio
- CRC/FEC/CACH/frame errors
- coherent IDLE/CSBK/DATA activity
- voice-stage diversity
- repetitive single-stage artifacts
- bounded sync-count support

Slot counts use only the bracketed active token, `[SLOT1]` or `[slot2]`.

PCM normalization uses both a percentile target and a hard peak-safe scale. The smaller scale is selected, so default PCM16 output has zero clipped samples. Reports preserve whether the cap was applied and how many samples would otherwise have clipped.

## Phase 5 — persistent inventory

Import one Phase 4/4.1 decode tree:

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

Phase 5 parses the selected best-polarity logs into:

- signed sync, Color Code and active-slot events
- IDLE, CSBK, DATA, VOICE and VC1–VC6 events
- Activity Update states
- explicit Talkgroup/Target and Radio/Source IDs
- vendor data and network-state evidence
- decoder errors
- correlated per-slot non-idle sessions

Outputs:

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

The SQLite database contains `runs`, `attempts`, `events`, `sessions` and `channels` tables.

Re-importing the same `run_id` replaces that run, so imports are idempotent. A different run ID is accumulated into the same persistent channel inventory.

DSD-FME clock strings are preserved as decoder-clock evidence. They are not treated as guaranteed original RF capture timestamps.

## Validated short-capture inventory

| Frequency | Color Code | Activity |
|---:|---:|---|
| 162.525000 MHz | 8 | CSBK/data |
| 162.587500 MHz | 5 | CSBK/data |
| 164.300000 MHz | 7 | mostly idle |
| 164.325000 MHz | 6 | mostly idle |
| 164.537500 MHz | 8 | idle and Group Voice |
| 164.725000 MHz | 7 | idle/data |
| 165.625000 MHz | 6 | idle/data, degraded |
| 167.137500 MHz | 7 | idle/data |

The short source captures did not contain reliable Talkgroup or Radio IDs. Empty ID lists are retained and are not replaced by guesses.

## IQ orientation

The original recordings use the conventional `IQ` assumption, but statistics alone cannot prove orientation. Phase 3 preserves the mirrored `QI` alternative. DSD-FME `-xr` symbol inversion is a separate question from IQ/QI frequency orientation.

## Tests

```bash
pytest -q
ruff check .
```

The suite covers metadata parsing, spectrum processing, candidate detection, streamed DSP, peak-safe WAV output, DSD-FME quality parsing, polarity selection, active slots, event parsing, session correlation, idempotent SQLite import and cross-run aggregation.

## Field collection after Phase 5

For TG and Radio ID collection, prefer a targeted 250–500 kS/s recording centered on one confirmed channel for 5–15 minutes during activity. A 10 MS/s signed int16 complex recording is approximately 2.4 GB/minute and should not be the default long-capture mode.

Start with 164.537500 MHz because complete voice-stage activity was already observed. Preserve date/time, location, antenna, gain, center frequency, sample rate and power condition for every new run.

See [`docs/phase5-design.md`](docs/phase5-design.md) and [`docs/NEXT-CONVERSATION-HANDOFF.md`](docs/NEXT-CONVERSATION-HANDOFF.md).

## Passive scope

The project performs receive-side offline analysis only. It contains no transmit, injection, impersonation, authentication bypass, brute-force or decryption capability.
