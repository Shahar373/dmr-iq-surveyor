# DMR IQ Surveyor

Offline Python tooling for inspecting SDRconnect wideband IQ recordings and building a reproducible frequency inventory before DMR decoding.

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

DSD-FME decoder confirmation is not implemented yet. Phase 3 labels are spectral hypotheses only.

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

## Inspect the configured recordings

```bash
./scripts/run_shahar_recordings.sh
```

## Run Phase 2 spectrum analysis

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

## Run Phase 3 candidate detection

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

The detector scores integrated average and P95 SNR, occupancy, occupied width, equivalent width, spectral fill, symmetry, persistence, and peak concentration. `dmr_like_narrowband` is not a decoder confirmation.

## IQ order

The current recordings use the conventional `IQ` assumption. Statistics alone cannot prove channel order. Before geographic conclusions are made, compare one known off-center carrier with SDRconnect. If the spectrum is mirrored, rerun inspection and spectrum analysis with `QI` or change `inspection.assumed_iq_order` in the batch YAML.

## Tests

```bash
pytest
```

The tests cover RIFF/RF64 parsing, center-frequency fallback, batch resilience, frequency-axis construction, overlap logic, FFT tone placement, spectrum artifacts, candidate raster calculations, IQ mirror calculations, channel-shape classification, spur rejection, and candidate merging.
