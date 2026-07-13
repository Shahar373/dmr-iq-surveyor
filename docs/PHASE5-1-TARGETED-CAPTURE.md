# Phase 5.1 — targeted capture workflow

Phase 5.1 supports longer channel-centered recordings without reusing unsafe 10 MS/s filter settings at lower sample rates.

## Supported extraction profiles

| Profile | Required input rate | Complex intermediate rate | Intended use |
|---|---:|---:|---|
| `10m` | 10,000,000 S/s | 100,000 S/s | wideband discovery and multi-location surveys |
| `500k` | 500,000 S/s | 100,000 S/s | targeted long capture with extra tuning margin |
| `250k` | 250,000 S/s | 50,000 S/s | smallest supported targeted recording |
| `auto` | detected | detected | selects one of the exact rates above |

A profile-rate mismatch fails before IQ processing. The final discriminator output remains 48 kHz mono PCM16 with peak-safe normalization.

## First targeted channel

```text
frequency: 164.537500 MHz
Color Code: 8
```

This channel already produced coherent Group Voice and complete VC1–VC6 evidence in the archived short capture.

## Recording recommendation

Use SDRconnect to create a channel-centered signed int16 complex IQ WAV:

```text
center frequency: 164.537500 MHz
sample rate:      500 kS/s preferred; 250 kS/s supported
capture length:   5–15 minutes
AGC:              off
manual gain:      fixed and recorded
antenna:          vertical VHF antenna, fixed geometry
```

Use 500 kS/s for the first field run because it provides more tuning and filter margin. Move to 250 kS/s after the complete path is validated on real data.

Approximate storage:

```text
250 kS/s complex int16 ≈ 60 MB/minute
500 kS/s complex int16 ≈ 120 MB/minute
```

## Metadata

Copy and edit:

```bash
cp config/targeted_capture_metadata.example.yaml \
  config/my_targeted_capture.yaml
```

At minimum record:

- source start time with timezone;
- latitude, longitude and location name;
- antenna, height and polarization;
- SDR model;
- center frequency and sample rate;
- AGC state and manual gain;
- power source and undervoltage state;
- notes about observed activity.

Metadata is stored in `extraction_report.json`, `attempts.json`, `attempts.csv`, and the persistent SQLite `attempts.capture_metadata_json` column.

## One-command processing

```bash
chmod +x scripts/run_targeted_164537500.sh

./scripts/run_targeted_164537500.sh \
  /full/path/to/recording.wav \
  config/my_targeted_capture.yaml \
  field_20260720_site_a \
  auto
```

The helper runs:

1. exact profile validation;
2. narrowband extraction;
3. normal and inverted DSD-FME attempts;
4. evidence-quality polarity selection;
5. Phase 5 event/session inventory;
6. merge into `runs/inventory/dmr_inventory.sqlite3`.

Outputs:

```text
runs/targeted/RUN_ID/
├── targeted_run.md
├── targeted_run.json
├── decodes/
└── inventory/
```

## Direct CLI

```bash
dmr-surveyor targeted-decode \
  /full/path/to/recording.wav \
  --frequency 164537500 \
  --profile auto \
  --metadata config/my_targeted_capture.yaml \
  --run-id field_20260720_site_a \
  --output runs/targeted/field_20260720_site_a \
  --database runs/inventory/dmr_inventory.sqlite3
```

A known frequency is processed directly. Phase 2 and Phase 3 are not required.

## Standalone DSD-FME logs

Logs produced outside this project can be imported directly:

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

The importer does not invent extraction provenance. It records `extraction_profile=standalone-log` and preserves the supplied capture metadata.

## Raspberry Pi checks

Before recording:

```bash
vcgencmd get_throttled
free -h
df -h /path/to/recording/storage
```

Do not record or process while undervoltage flags are active. Recording reliability is more important than processing in the field; decoding can be performed later on stable power.

## Success criteria

A useful targeted run should show:

- DMR clean or degraded confirmation;
- stable CC8;
- multiple meaningful voice/control sessions;
- explicit Talkgroup/Target and Radio/Source IDs when those headers are captured;
- zero clipped discriminator samples;
- capture metadata present in the persistent inventory.

Empty ID lists remain valid when the necessary headers were not transmitted during the recording.
