# Phase 6 field procedure — 800 MHz reconnaissance capture

Short first-look capture at a new site, to exercise Phase 6A discovery end to end on real RF and start building history for that site. This is a **spectral candidate detection** session, not a P25 identification session — Phase 6B (decoder-confirmed P25) does not exist yet. Nothing produced here is a confirmed protocol.

Read `docs/FIELD-RECORDING-GUIDE.md` §3 (equipment) and §5 (Pi pre-flight checks) first — they apply unchanged. This document only covers what's different for an 800 MHz, Phase 6A capture.

## 1. Settings

```text
center frequency: 868.000000 MHz
sample rate:       10.000000 MS/s
format:            signed 16-bit complex IQ
AGC:               off
manual gain:       set per the two-step procedure below (unknown ahead of time at a new site)
capture length:    60-120 seconds
```

868.000000 MHz is the geometric center of 866-870 MHz, chosen with no reference to any known frequency — Phase 6A's discovery has to see RF before anything else, and centering on a "known" candidate would bias what gets covered. 10 MS/s gives ±5 MHz of Nyquist width, comfortably covering the requested 4 MHz band with margin on both sides; this reuses the already-validated `10m` extraction-profile chain used elsewhere in the project, unlike a still-unconfirmed 5 MS/s capture.

## 2. Gain: two-step procedure

The signal environment at a new site is unknown, and a nearby public-safety transmitter can be strong enough to overload the front end. Do not commit to the full capture on a guess.

1. Set AGC off and a moderate starting gain (roughly mid-range gain reduction — not the receiver's most sensitive setting).
2. Record a short **10-15 second** test capture at that gain.
3. Immediately check it:

   ```bash
   dmr-surveyor inspect /path/to/test_capture.wav -o /tmp/inspect_test
   ```

   Read the warnings in the table output and in `report.md`. Phase 1 inspection flags clipping and zero-region issues explicitly.
4. If clipping or overload warnings appear, increase gain reduction (lower the gain) and repeat from step 2.
5. Once a test capture is clean, **that gain value is your baseline for this site** — do not change it again for the real capture, and write it down. It becomes `gain` in the site profile (`config/sites/<site_id>.yaml`), matching the project's existing rule that a site profile with no recorded gain is flagged in reports as not gain-comparable.

## 3. Filename

Keep the SDRconnect default naming convention:

```text
SDRconnect_IQ_<YYYYMMDD>_<HHMMSS>_868000000HZ.wav
```

The `YYYYMMDD_HHMMSS` and `<freq>HZ` pattern is not decorative: Phase 6A's `resolve_capture_time()` falls back to parsing it from the filename when the SDRplay `auxi` metadata chunk is missing or unreadable, and the frequency suffix is a documented fallback in `iq/metadata.py`. Do not rename the file before the real capture, or before processing it.

## 4. Capture

1. Complete the equipment and Pi pre-flight checks from `docs/FIELD-RECORDING-GUIDE.md` §3 and §5 (`vcgencmd get_throttled`, `df -h .`, power stable).
2. Confirm the gain from step 2 above is still set, AGC off.
3. Record 60-120 seconds at the settings in §1.
4. Re-check `vcgencmd get_throttled` before leaving the site.
5. Note in a text file, at minimum: site label, approximate location (as much or as little precision as you want to keep — latitude/longitude are optional in the site profile), antenna used, receiver gain value, local and UTC start time, any obvious terrain/obstacles.

## 5. Processing

Pick a short `site_id` (lowercase, no spaces — it becomes both a database key and a filename component), e.g. `site2`.

```bash
# sanity check the real capture the same way as the test capture
dmr-surveyor inspect /path/to/recording.wav -o runs/inspect_site2

# create the site profile once, from the template
cp config/sites/home.example.yaml config/sites/site2.yaml
# edit config/sites/site2.yaml: site_id, label, receiver, gain_mode: manual,
# gain: <the value confirmed in step 2>, notes

# run the survey with the reconnaissance band profile (short-capture segmentation)
dmr-surveyor survey run /path/to/recording.wav \
  --band central_800_recon --site site2 \
  --run-id site2_<YYYYMMDD> \
  --output runs/survey/site2_<YYYYMMDD>

dmr-surveyor survey show site2_<YYYYMMDD>
```

`central_800_recon` (not `central_800`) is tuned for a 60-120 second capture — 1-second segments every 5 seconds, instead of the full profile's 2s/10s, so `persistence` still carries a meaningful signal over a short window. Both profiles use identical detection thresholds; only segmentation differs.

## 6. What to expect, and what not to conclude

- A signal with `persistence` near 1.0 and high `occupancy_pct` over the whole capture is consistent with a continuously-transmitting channel (e.g. a P25 control channel almost always behaves this way) — but this is a **spectral hypothesis** (`spectral_class`), not a confirmed protocol. `classification` will read `unknown` for everything; that is correct behaviour, not a bug.
- 60-120 seconds is enough to see a control-channel-like persistent carrier if one is in range with adequate SNR. It is *not* enough to characterize intermittent traffic-channel activity — that needs a longer capture, a separate session.
- If nothing is detected, that means nothing crossed the detection thresholds in this window at this gain and antenna position — not that no P25 system exists in range. Consider a longer follow-up capture or a gain/antenna change before concluding absence.

## 7. What to send back for analysis

This assistant runs in a remote environment with no access to the Pi or the recording. After processing, share:

- the console output of `dmr-surveyor survey run ...` and `dmr-surveyor survey show ...`;
- `runs/survey/site2_<YYYYMMDD>/reports/report.md` (or the full directory);
- the gain value and any field notes from §4.5.

That is enough to review the candidates found, decide on follow-up capture parameters, and plan Phase 6B (P25 decoder evidence) work against the strongest candidate.

## 8. One-command capture with automatic GPS (`dmr-surveyor survey capture`)

The steps above assume SDRconnect recorded the file and `survey run` processes it afterwards.
`dmr-surveyor survey capture` collapses recording + survey into one command, driving the RSP1B
directly via SoapySDR, and can fetch the capture-site coordinates from a phone automatically instead
of you typing them into a site profile.

**Storage speed matters more than anything else here.** 10 MS/s IQ needs 40 MB/s of sustained write
throughput; a slow SD card cannot keep up (confirmed on this project's Pi 5: an SD card measured at
~10 MB/s truncated a 15-second capture to ~13.5s even with an enlarged buffer). Before relying on
this command for a real capture, retest write speed at the actual output path
(`dd if=/dev/zero of=<output_dir>/writetest.bin bs=4M count=500 oflag=direct status=progress`) and
either use faster storage (a USB3 drive on the Pi 5) or drop `--sample-rate` to something the
storage measurably sustains with margin.

### 8.1 Phone-side GPS server (one-time setup, Android + Termux)

1. Install the **Termux** and **Termux:API** apps from F-Droid (not the Play Store — those builds
   are outdated and can't install packages reliably).
2. Open Termux and run `termux-setup-storage` (grants access to your Downloads folder; tap "Allow").
3. Download `scripts/dmr_gps_setup.sh` to the phone (e.g. from this chat, or `git clone` the repo)
   so it lands in Downloads, then in Termux:

   ```bash
   bash storage/downloads/dmr_gps_setup.sh
   ```

   This installs `python` + `termux-api`, writes `phone_gps_server.py` to the Termux home directory,
   adds a one-tap "GPS Server" launcher (visible as a home-screen widget if the separate
   **Termux:Widget** app is installed), and prints the phone's IP addresses.

From then on, before each capture: tap the widget, or run `python phone_gps_server.py` in Termux. It
serves a fresh GPS fix (via `termux-location`) on every request — leaving it running for a whole
field session or restarting it before each capture both work identically, since nothing is cached.
Use the phone's hotspot IP it prints (or check Android Settings -> Hotspot) to build the URL below.

### 8.2 Capture command

```bash
dmr-surveyor survey capture \
  ~/Projects/dmr-iq-surveyor/runs/recordings \
  --band central_800_recon \
  --site config/sites/home.example.yaml \
  --center-frequency 868000000 \
  --sample-rate 10000000 \
  --duration 90 \
  --gain 40 \
  --no-agc \
  --gps-url http://<phone-hotspot-ip>:8765/location
```

If the phone server is unreachable, the capture and survey still complete — the run is stored with
`gps_source=fetch_failed` and the reason recorded (never a silent gap). `--latitude`/`--longitude`
are available as a manual override (e.g. to reuse a known fixed position) and take precedence over
`--gps-url` when both are given. `dmr-surveyor survey show <run_id>` and `reports/report.md` show the
resolved GPS coordinates and their source alongside the RF observations.
