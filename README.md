# DMR IQ Surveyor

Offline Python tooling for inspecting SDRconnect wideband IQ recordings, building a reproducible frequency inventory, extracting narrowband channels, and testing them with DSD-FME.

Implemented stages:

- RIFF/RF64 and SDRplay metadata inspection
- memory-mapped IQ reading
- bounded integrity and clipping checks
- streamed FFT analysis
- average, max-hold, and deterministic percentile spectra
- adaptive local noise-floor estimation
- per-frequency occupancy
- reduced, time-binned waterfall output
- independent batch processing for multiple recordings
- Phase 3 narrowband candidate detection and ranking
- spur, wideband, intermittent, and DMR-like preliminary classes
- IQ-mirror frequency preservation while channel order remains assumed
- Phase 4 streamed narrowband extraction
- 48 kHz mono discriminator WAV generation
- optional DSD-FME normal/inverted DMR attempts with captured evidence
- Phase 4.1 evidence-quality polarity scoring and active-slot parsing
- peak-safe PCM16 normalization with zero clipped output samples by default

The chronological project record is in [`docs/development-history.md`](docs/development-history.md).

## Install on Raspberry Pi OS, Debian, or Ubuntu

```bash
sudo apt update
sudo apt install -y git python3 python3-venv python3-pip
cd dmr-iq-surveyor
./scripts/bootstrap.sh
```

Activate the environment in future terminal sessions:

```bash
source .venv/bin/activate
```

DSD-FME is optional. Without it, Phase 4 still creates discriminator WAV files and records `decoder_unavailable` instead of failing the extraction.

## Run the configured workflow

### Phase 1 — inspect recordings

```bash
./scripts/run_shahar_recordings.sh
```

### Phase 2 — spectrum analysis

```bash
./scripts/run_shahar_spectrum.sh
```

The two recordings are analyzed independently. They are not concatenated across the gap between files.

Single-recording example:

```bash
dmr-surveyor spectrum \
  /full/path/to/recording.wav \
  --output runs/my-recording/spectrum
```

Batch example:

```bash
dmr-surveyor spectrum-batch config/shahar_recordings.yaml
```

### Phase 3 — candidate detection

```bash
./scripts/run_shahar_detection.sh
```

Single-spectrum example:

```bash
dmr-surveyor detect \
  runs/my-recording/spectrum \
  --output runs/my-recording/candidates
```

Batch example:

```bash
dmr-surveyor detect-batch config/shahar_recordings.yaml
```

The batch detector merges evidence from separate recordings without merging adjacent 12.5 kHz or 25 kHz channels. It keeps signals seen in only one recording and records both the assumed IQ frequency and the mirrored QI alternative.

### Phase 4 — narrowband extraction and DSD-FME

```bash
chmod +x scripts/run_shahar_decode.sh
./scripts/run_shahar_decode.sh
```

The helper runs with reduced CPU and I/O priority and limits numerical-library thread counts to one. Candidates and source recordings are processed sequentially.

Extract one frequency manually:

```bash
dmr-surveyor extract-channel \
  /full/path/to/recording.wav \
  --frequency 165625000 \
  --output runs/manual-165625000
```

Decode an existing discriminator WAV:

```bash
dmr-surveyor decode-channel \
  runs/manual-165625000/discriminator.wav \
  --output runs/manual-165625000/decoder
```

Run the configured Phase 3 candidates:

```bash
dmr-surveyor decode-batch config/shahar_recordings.yaml
```

The upstream DSD-FME examples accept 48 kHz mono WAV input with `dsd-fme -i filename.wav`. The project adds `-fs` for DMR and also tries `-xr` when configured.

## Decoder evidence quality

Phase 4.1 does not treat a process exit code or a single generic DMR-looking line as confirmation. Only signed `Sync: +DMR` or `Sync: -DMR` lines count as explicit sync evidence, and attempts are classified as:

```text
dmr_sync_only
dmr_confirmed_degraded
dmr_confirmed_clean
```

Quality scoring considers:

- numeric Color Code ratio;
- dominant Color Code consistency;
- clean signed-sync ratio;
- CRC/FEC/CACH/frame error ratio;
- coherent IDLE/CSBK/DATA activity;
- voice-stage diversity;
- repetitive single-stage `VC1` artifacts;
- bounded sync-count support.

Normal and inverted profiles are selected by this quality score, not by raw sync-line count. Every component is retained in the JSON report.

DSD-FME prints both slot labels on each status line. Slot counts use only the bracketed active token, such as `[SLOT1]` or `[slot2]`.

## PCM normalization

Discriminator audio is median-centered and given a percentile-derived target level. A second peak-safe scale caps the absolute peak at `output_peak_fraction`, preventing PCM16 clipping. Extraction reports record:

- whether the peak cap was applied;
- samples that would have clipped without it;
- actual clipped samples;
- selected scale and final PCM peak.

The default output has zero clipped samples.

## Spectrum artifacts

Each recording produces:

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

Power is reported as relative `dBFS/Hz`; it is not calibrated dBm. DC and receiver-edge regions are flagged in every CSV and shaded in plots rather than silently removed.

## Candidate artifacts

Phase 3 produces:

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

The detector scores integrated average and P95 SNR, occupancy, occupied width, equivalent width, spectral fill, symmetry, persistence, and peak concentration. `dmr_like_narrowband` is a spectral hypothesis, not decoder confirmation.

## Phase 4 artifacts

Each candidate, recording, and IQ hypothesis receives an independent directory:

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

The batch root also contains `decode_batch_summary.csv`, `decode_batch_summary.json`, and `decode_batch_report.md`. Phase 4.1 reports add quality score, dominant Color Code, valid-CC ratio, dominant-CC consistency, error ratio, and active-slot counts.

## IQ order

The current recordings use the conventional `IQ` assumption. Statistics alone cannot prove channel order. Phase 3 preserves the mirrored QI frequency, and Phase 4 can process `QI` as a separate hypothesis by adding it to `phase4.iq_hypotheses` in the YAML configuration.

DSD-FME `-xr` symbol inversion is separate from the IQ/QI frequency-orientation question.

## Tests

```bash
pytest
ruff check .
```

The tests cover RIFF/RF64 parsing, center-frequency fallback, batch resilience, frequency-axis construction, overlap logic, FFT tone placement, spectrum artifacts, candidate raster calculations, IQ mirror calculations, channel-shape classification, spur rejection, candidate merging, phase-continuous mixing, streamed FM discrimination, adjacent-channel rejection, peak-safe 48 kHz WAV generation, DSD-FME quality parsing, polarity scoring, active-slot parsing, and missing-decoder handling.

## Passive scope

The project performs offline receive-side analysis only. It contains no transmit, authentication, impersonation, injection, brute-force, or decryption capability.
