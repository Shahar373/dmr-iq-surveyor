# Field card — 800 MHz survey session

One page, in order. Full reasoning lives in `docs/PHASE6-FIELD-800MHZ.md`; the fallback path is
`docs/FALLBACK-RSP-RECORDER.md`.

---

## A. At home, before leaving

Everything here needs mains power and a real network. None of it can be done at the site.

```bash
cd ~/Projects/dmr-iq-surveyor
git checkout main && git pull origin main
source .venv/bin/activate
pip install -e '.[dev]'

# Installs SoapySDR, builds the SDRplay driver module, links it into this venv.
# The RSP working with other tools is NOT evidence this is already present.
bash scripts/pi_soapysdr_setup.sh
```

Then measure what the storage can actually do, at the path you will record into:

```bash
dmr-surveyor survey preflight ~/Projects/dmr-iq-surveyor/runs/recordings \
  --band central_800_recon --center-frequency 868000000 \
  --sample-rate 6000000 --duration 90
```

**Read the "safe up to ~N MS/s" figure and pick the rate from this table.** The usable RF span is set
by the analog IF filter, which comes in fixed steps — rates between them cost storage and buy
nothing.

| Sustained write measured | Use | Usable span | Band coverage |
|---|---|---|---|
| ≥ 30 MB/s | `--sample-rate 6000000` | 6 MHz | full, with margin |
| ≥ 25 MB/s | `--sample-rate 5000000` | 5 MHz | full |
| ≥ 10 MB/s | `--sample-rate 2000000` | 1.536 MHz | partial — see §C |
| below that | do not capture to this path | | |

A USB3 SSD on the Pi 5 is the difference between full-band coverage in one capture and tiling the
band across three. If one is available, use it and record to it.

Finally, a 10-second rehearsal on real hardware — the only thing that converts this command from
"never run against this RSP" to "run once against this RSP":

```bash
dmr-surveyor survey capture ~/Projects/dmr-iq-surveyor/runs/smoketest \
  --band central_800_recon --site config/sites/home.example.yaml \
  --center-frequency 868000000 --sample-rate <chosen> \
  --duration 10 --if-gr 40 --lna-state 4 --no-agc
```

Check in the output table: **Actual duration** matches requested, **Dropped buffers** absent, **Gain
applied** shows `IFGR=40, RFGR=4`. If any of those is wrong, fix it here, not at the site.

Phone: install Termux + Termux:API from F-Droid, run `bash storage/downloads/dmr_gps_setup.sh` once.

---

## B. On arrival at each site

```bash
# 1. Phone: tap the "GPS Server" widget (or: python phone_gps_server.py)
#    Note the IP it prints.

# 2. Preflight at this site's actual output path
dmr-surveyor survey preflight ~/Projects/dmr-iq-surveyor/runs/recordings \
  --band central_800_recon --center-frequency 868000000 \
  --sample-rate <chosen> --duration 90 \
  --gps-url http://<phone-ip>:8765/location
```

Must print **GO** (or GO-with-caveats where the caveat is one you already accepted). If it says
NO-GO, fix that row before capturing.

```bash
# 3. Gain check: 15 seconds, then look at it. Do NOT skip this.
dmr-surveyor survey capture ~/Projects/dmr-iq-surveyor/runs/recordings \
  --band central_800_recon --site config/sites/home.example.yaml \
  --site-id <stop_name> \
  --center-frequency 868000000 --sample-rate <chosen> \
  --duration 15 --if-gr 40 --lna-state 4 --no-agc \
  --gps-url http://<phone-ip>:8765/location \
  --run-id <stop_name>_probe

dmr-surveyor inspect ~/Projects/dmr-iq-surveyor/runs/recordings/SDRconnect_IQ_*.wav \
  -o /tmp/gaincheck
```

Read the **Warnings** section. Clipping or near-clip warnings mean the front end is overloaded:
**raise `--if-gr`** (more reduction = less sensitive), try 45 then 50. All-zero or near-zero channel
warnings mean too little signal: **lower `--if-gr`**, try 35 then 30. Repeat until clean, then keep
that value for the rest of the session and write it down.

```bash
# 4. The real capture
dmr-surveyor survey capture ~/Projects/dmr-iq-surveyor/runs/recordings \
  --band central_800_recon --site config/sites/home.example.yaml \
  --site-id <stop_name> \
  --center-frequency 868000000 --sample-rate <chosen> \
  --duration 90 --if-gr <confirmed> --lna-state 4 --no-agc \
  --gps-url http://<phone-ip>:8765/location \
  --run-id <stop_name>_$(date +%Y%m%d)

# 5. Look at the result before packing up
dmr-surveyor survey show <stop_name>_$(date +%Y%m%d)
vcgencmd get_throttled          # 0x0 means power and thermals were fine
```

`--site-id` matters: one profile describes the equipment, but each stop must be a distinct site or
`survey compare` will treat two different locations as the same place.

---

## C. If you are stuck at 2 MS/s

1.536 MHz of usable span does not cover 866–870 MHz. Either accept `coverage_status: partial` from a
single capture centred on 868.000 MHz, or tile the band with three captures:

| Capture | `--center-frequency` | Covers |
|---|---|---|
| 1 | `866750000` | 865.98 – 867.52 MHz |
| 2 | `868200000` | 867.43 – 868.97 MHz |
| 3 | `869250000` | 868.48 – 870.02 MHz |

Use the same `--site-id` for all three and distinct `--run-id`s (`<stop>_lo`, `<stop>_mid`,
`<stop>_hi`). Partial coverage is reported honestly per run, never silently skipped.

---

## D. When something goes wrong

| Symptom | Do this |
|---|---|
| `SoapySDR Python bindings are not importable` | `bash scripts/pi_soapysdr_setup.sh` |
| `No SoapySDR device matched` / `activateStream failed` | `sudo systemctl restart sdrplay`, then re-run preflight |
| `Dropped buffers: N` in the output | Storage could not keep up. Lower `--sample-rate` one step and redo the capture |
| `Actual duration` well below requested | Same cause; also check `df -h` for a full disk |
| Capture failed but a WAV exists | The message names it — run `dmr-surveyor survey run <file> --band ... --site ...` on it, the data is fine |
| Nothing works and time is short | Fall back to `docs/FALLBACK-RSP-RECORDER.md`; it shares the entire analysis half |

---

## E. Before leaving each site

- `runs/survey/<run_id>/reports/report.md` exists and lists observations.
- The GPS row in the capture output showed real coordinates, not `not configured`.
- `vcgencmd get_throttled` returned `0x0`.
- Note the antenna used and anything about the location a report cannot capture (terrain, obstacles,
  how high the antenna was, what was nearby).

Nothing here confirms a protocol. Everything found is a **spectral candidate**; `classification` will
read `unknown` for all of it, which is correct — decoder-backed P25 identification is a later phase.
