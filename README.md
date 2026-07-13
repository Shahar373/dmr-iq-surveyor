# DMR IQ Surveyor

Offline Python tooling for inspecting SDRconnect wideband IQ recordings and building a reproducible frequency inventory before DMR decoding.

Implemented stages:

- RIFF/RF64 and SDRplay metadata inspection
- memory-mapped IQ reading
- bounded integrity and clipping checks
- streamed FFT analysis
- average and max-hold spectra
- deterministic percentile spectrum
- adaptive local noise-floor estimation
- per-frequency occupancy
- reduced, time-binned waterfall output
- independent batch processing for multiple recordings

DMR candidate extraction and DSD-FME classification are not implemented yet.

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

## IQ order

The current recordings use the conventional `IQ` assumption. Statistics alone cannot prove channel order. Before geographic conclusions are made, compare one known off-center carrier with SDRconnect. If the spectrum is mirrored, rerun with `--iq-order QI` or change `inspection.assumed_iq_order` in the batch YAML.

## Tests

```bash
pytest
```

The tests cover RIFF/RF64 parsing, center-frequency fallback, batch resilience, frequency-axis construction, overlap logic, FFT tone placement, spectrum artifacts, and independent batch processing.
