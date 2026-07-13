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
- Phase 5.1: validated 10m/500k/250k targeted-capture profiles, metadata and standalone-log import

## Project documentation

- [`docs/development-history.md`](docs/development-history.md)
- [`docs/phase4-design.md`](docs/phase4-design.md)
- [`docs/phase5-design.md`](docs/phase5-design.md)
- [`docs/phase5-session-semantics.md`](docs/phase5-session-semantics.md)
- [`docs/PHASE5-1-TARGETED-CAPTURE.md`](docs/PHASE5-1-TARGETED-CAPTURE.md)
- [`docs/FIELD-RECORDING-GUIDE.md`](docs/FIELD-RECORDING-GUIDE.md)
- [`docs/TRANSMITTER-LOCATION-STUDY.md`](docs/TRANSMITTER-LOCATION-STUDY.md)
- [`docs/FIELD-SESSION-METADATA-TEMPLATE.csv`](docs/FIELD-SESSION-METADATA-TEMPLATE.csv)
- [`docs/NEXT-CONVERSATION-HANDOFF.md`](docs/NEXT-CONVERSATION-HANDOFF.md)

The field guide is the authoritative checklist before every future recording session.

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

Spectrum artifacts include average, max-hold and percentile spectra, local noise floor, occupancy and a reduced waterfall.

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

## Phase 4 — extraction and DSD-FME

Extract one 10 MS/s channel:

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
  -> validated FIR decimation profile
  -> 50 or 100 kHz complex baseband
  -> channel low-pass
  -> FM phase discriminator
  -> rational resampling
  -> 48 kHz mono PCM16
  -> DSD-FME normal and inverted profiles
```

## Phase 4.1 — decoder evidence quality

Only signed `Sync: +DMR` or `Sync: -DMR` lines count as explicit sync evidence. Attempts are classified as:

```text
dmr_sync_only
dmr_confirmed_degraded
dmr_confirmed_clean
```

Polarity scoring considers numeric Color Code ratio, dominant-CC consistency, clean sync ratio, decoder errors, coherent activity, voice-stage diversity and false inverted-decoding patterns.

Slot counts use only the bracketed active token, `[SLOT1]` or `[slot2]`.

PCM normalization uses a percentile target plus a hard peak-safe scale. The default PCM16 output contains zero clipped samples.

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

Phase 5 parses selected best-polarity logs into:

- signed sync, Color Code and active-slot events;
- IDLE, CSBK, DATA, VOICE and VC1–VC6 events;
- Activity Update states;
- explicit Talkgroup/Target and Radio/Source IDs;
- vendor-data and network-state evidence;
- decoder errors;
- correlated per-slot sessions.

The SQLite database contains:

```text
runs
attempts
events
sessions
channels
```

Re-importing the same `run_id` replaces that run. A different run ID accumulates into the same persistent channel inventory.

Sessions made entirely of decoder errors are retained as `error_only`. Reports distinguish total, meaningful, and error-only session counts.

DSD-FME clock strings are preserved as decoder-clock evidence. They are not treated as guaranteed original RF capture timestamps.

## Phase 5.1 — targeted known-frequency capture

Supported exact-rate profiles:

| Profile | Input rate | Intermediate rate |
|---|---:|---:|
| `10m` | 10,000,000 S/s | 100,000 S/s |
| `500k` | 500,000 S/s | 100,000 S/s |
| `250k` | 250,000 S/s | 50,000 S/s |
| `auto` | detected | detected |

A rate/profile mismatch fails before IQ processing.

Process a known channel without Phase 2 or Phase 3:

```bash
dmr-surveyor targeted-decode \
  /path/to/channel-centered.wav \
  --frequency 164537500 \
  --profile auto \
  --metadata config/my_targeted_capture.yaml \
  --run-id field_20260720_site_a \
  --output runs/targeted/field_20260720_site_a \
  --database runs/inventory/dmr_inventory.sqlite3
```

Helper:

```bash
chmod +x scripts/run_targeted_164537500.sh
./scripts/run_targeted_164537500.sh \
  /path/to/channel-centered.wav \
  config/my_targeted_capture.yaml \
  field_20260720_site_a \
  auto
```

Capture metadata is preserved in extraction reports, Phase 5 exports, and `attempts.capture_metadata_json` in SQLite.

Import a DSD-FME log produced elsewhere:

```bash
dmr-surveyor inventory-import-log \
  /path/to/dsd-fme.log \
  --frequency 164537500 \
  --run-id external_20260720_site_a \
  --recording-id site_a \
  --metadata config/my_targeted_capture.yaml \
  --output runs/standalone/external_20260720_site_a \
  --database runs/inventory/dmr_inventory.sqlite3
```

See [`docs/PHASE5-1-TARGETED-CAPTURE.md`](docs/PHASE5-1-TARGETED-CAPTURE.md).

## Does Phase 5 require another field recording?

No. Phase 5 can be run and validated using the existing Phase 4.1 outputs.

New recordings are needed only to add new evidence, such as:

- longer voice/control activity for Talkgroup and Radio IDs;
- measurements from additional locations;
- transmitter coverage and location studies;
- cleaner recordings of degraded or edge-of-passband channels.

## Field recording modes

### Multi-location survey

Use short, identical wideband captures at multiple sites to compare all confirmed channels.

Recommended first campaign:

```text
center frequency: 164.831250 MHz
sample rate:      10.000 MS/s
capture length:   15–20 seconds
repeats:          2 per site
sites:            8–12
AGC:              off
manual gain:      identical at all sites
```

One recording covers all eight confirmed channels simultaneously. See [`docs/FIELD-RECORDING-GUIDE.md`](docs/FIELD-RECORDING-GUIDE.md).

### Targeted identity capture

The first targeted channel is:

```text
164.537500 MHz
Color Code 8
```

Recommended first profile:

```text
center frequency: 164.537500 MHz
sample rate:      500 kS/s
capture length:   5–15 minutes
AGC:              off
manual gain:      fixed and recorded
```

The `500k` profile is now implemented. `250k` is also supported, but 500 kS/s provides more margin for the first real targeted run.

### Transmitter location study

Sequential recordings from one receiver can build a coverage heatmap and reduce the probable search area. RSSI alone normally cannot produce a precise coordinate.

Preferred progression:

1. multi-location RSSI heatmap;
2. closer repeat measurements;
3. directional bearings from at least three sites;
4. simultaneous synchronized TDOA or coherent AoA only when higher precision is required.

See [`docs/TRANSMITTER-LOCATION-STUDY.md`](docs/TRANSMITTER-LOCATION-STUDY.md).

## Confirmed short-capture inventory

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

The suite covers metadata parsing, spectrum processing, candidate detection, streamed DSP, 10m/500k/250k profiles, peak-safe WAV output, DSD-FME quality parsing, polarity selection, active slots, event parsing, session semantics, capture metadata migration, standalone-log import, idempotent SQLite import and cross-run aggregation.

## Passive scope

The project performs receive-side offline analysis only. It contains no transmit, injection, impersonation, authentication bypass, brute-force or decryption capability.
