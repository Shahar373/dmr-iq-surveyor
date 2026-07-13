# DMR IQ Surveyor

Offline Python tooling for inspecting SDRconnect wideband IQ recordings and, in later phases, detecting and classifying DMR channels.

This repository currently implements **Milestone 1 only**:

- RIFF and RF64 parsing
- `ds64`, `fmt `, and SDRplay-style `auxi` metadata parsing
- memory-mapped IQ access
- bounded sample statistics
- corruption and clipping warnings
- diagnostic IQ plots
- reproducible inspection artifacts

It does not yet detect channels or run DSD-FME.

## Install

```bash
sudo apt update
sudo apt install -y git python3 python3-venv python3-pip
./scripts/bootstrap.sh
source .venv/bin/activate
```

## Inspect configured recordings

```bash
./scripts/run_shahar_recordings.sh
```

Source IQ recordings remain outside Git. Generated `runs/` output and large RF files are ignored.
