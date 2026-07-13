# DMR IQ Surveyor — field recording guide

This document is the operational checklist for future field sessions. Read it before leaving home and keep a copy available offline.

## 1. Does Phase 5 require a new recording?

No. Phase 5 can be run and validated entirely from the existing Phase 4.1 decode tree.

A new recording is needed only to add evidence:

- recover Talkgroup or Radio IDs;
- observe longer voice, data or control activity;
- compare strength at additional locations;
- estimate transmitter coverage or probable location;
- verify IQ/QI orientation;
- replace degraded or edge-of-passband captures.

## 2. Choose one session objective

Do not mix objectives into an uncontrolled capture.

### Mode A — multi-location survey

Purpose: compare all eight confirmed carriers and build a coverage/geolocation dataset.

```text
center frequency: 164.831250 MHz
sample rate:      10.000 MS/s
format:           signed 16-bit complex IQ
capture length:   15–20 seconds
repeats:          2 per site
sites:            8–12
profile later:    10m
```

One recording covers all eight confirmed channels. Approximate storage:

```text
15 seconds ≈ 600 MB
20 seconds ≈ 800 MB
2 × 20 seconds ≈ 1.6 GB per site
10 sites ≈ 16 GB
```

### Mode B — targeted identity capture

Purpose: recover TG and Radio IDs from one known channel.

Start with:

```text
frequency:        164.537500 MHz
Color Code:       8
center frequency: 164.537500 MHz
sample rate:      500 kS/s preferred
capture length:   5–15 minutes
profile later:    500k or auto
```

The `500k` and `250k` extraction profiles are implemented in Phase 5.1. Use 500 kS/s for the first real targeted capture because it provides more tuning margin. Approximate storage:

```text
500 kS/s complex int16 ≈ 120 MB/minute
250 kS/s complex int16 ≈ 60 MB/minute
```

See `docs/PHASE5-1-TARGETED-CAPTURE.md`.

### Mode C — directional bearing survey

Purpose: estimate transmitter bearing from several sites.

Use a directional VHF antenna, fixed manual gain and a reproducible rotation procedure. See `docs/TRANSMITTER-LOCATION-STUDY.md`.

## 3. Equipment checklist

Required:

- Raspberry Pi 5 and sufficient storage;
- SDRplay RSP1B-class receiver;
- the same VHF antenna and coax for the full campaign;
- stable USB-C PD power or UPS HAT;
- phone with GPS and camera;
- repeatable antenna mount and height;
- metadata template or notebook.

Recommended:

- second power bank reserved for the Pi/SDR;
- external storage for backup;
- compass;
- 50-ohm terminator for receiver-noise checks;
- for bearing work: VHF Yagi/log-periodic and step attenuator.

## 4. Receiver settings that must stay fixed

For comparisons between sites, keep constant:

- AGC off;
- LNA state;
- IF gain reduction/manual gain;
- RF bandwidth;
- sample rate and center frequency;
- antenna, coax and adapters;
- antenna height, orientation and polarization;
- IQ format and SDRconnect version;
- filters, preamplifiers and bias-T state.

Choose gain at the first site with headroom for the strongest carrier. Do not optimize gain separately at each location. If overload occurs, preserve the standard capture and add a clearly labelled lower-gain repeat.

## 5. Raspberry Pi checks

Before leaving:

```bash
cd ~/Projects/dmr-iq-surveyor
git checkout main
git pull --ff-only origin main
source .venv/bin/activate
python -m pip install -e '.[dev]'

dmr-surveyor --help
vcgencmd get_throttled
free -h
df -h .
timedatectl status
```

Desired power result:

```text
throttled=0x0
```

Correct any current undervoltage before recording. Capture first and process later on stable power.

## 6. Site selection for geolocation campaigns

Use 8–12 sites around the suspected region, not along one line.

Include:

- north, south, east and west geometry;
- at least one high open site;
- at least one low urban site;
- expected strong and weak sites;
- initial spacing of roughly 1–3 km.

After the first heatmap, add 6–10 refinement sites with 200–500 m spacing around the strongest plausible region.

Avoid recording inside a vehicle, beside large metal structures, or where antenna geometry cannot be repeated. Use only safe and legal locations.

## 7. Per-site procedure for Mode A

1. Stop in a safe, legal location.
2. Record GPS latitude, longitude, altitude and reported accuracy.
3. Photograph the antenna placement and horizon.
4. Install the antenna at the standard height and orientation.
5. Allow at least 60 seconds for receiver stabilization.
6. Confirm all fixed settings and AGC off.
7. Run:

   ```bash
   vcgencmd get_throttled
   df -h .
   ```

8. Record the first 15–20 second 10 MS/s IQ file.
9. Wait 30–60 seconds.
10. Record an identical second file.
11. Verify file size and metadata.
12. Record obstacles, interference, terrain and weather.
13. Recheck undervoltage before leaving.

## 8. Procedure for Mode B

1. Center SDRconnect exactly at 164.537500 MHz.
2. Select 500 kS/s for the first targeted run.
3. Use signed 16-bit complex IQ WAV.
4. Disable AGC and record manual gain.
5. Start during a period where voice activity is likely.
6. Record 5–15 minutes.
7. Preserve the original filename and center-frequency metadata.
8. Fill a metadata YAML based on:

   ```bash
   cp config/targeted_capture_metadata.example.yaml \
     config/my_targeted_capture.yaml
   ```

9. Process later:

   ```bash
   chmod +x scripts/run_targeted_164537500.sh
   ./scripts/run_targeted_164537500.sh \
     /full/path/to/recording.wav \
     config/my_targeted_capture.yaml \
     field_YYYYMMDD_site_a \
     auto
   ```

## 9. File naming

Multi-location campaign example:

```text
field-data/YYYYMMDD_geolocation_campaign_01/
├── sites.csv
├── site_01/
│   ├── site_01_repeat_01_164831250HZ_10000000SPS.wav
│   └── site_01_repeat_02_164831250HZ_10000000SPS.wav
└── site_02/
```

Targeted capture example:

```text
field-data/YYYYMMDD_targeted_164537500/
├── site_a_164537500HZ_500000SPS.wav
└── site_a_metadata.yaml
```

Use a new run ID for every campaign or targeted recording so Phase 5 accumulates observations rather than replacing a prior run.

## 10. Metadata required

Record at minimum:

```text
campaign_id / run_id
site_id / recording_id
local and UTC datetime
latitude, longitude, altitude, GPS accuracy
center_frequency_hz
sample_rate_hz
capture_duration_s
receiver model and serial
SDRconnect version
AGC, LNA and manual gain
RF bandwidth
antenna, height, orientation and polarization
coax and filters/LNA
power source
vcgencmd before and after
weather, terrain and obstacles
file path and SHA-256
notes
```

Templates:

- `docs/FIELD-SESSION-METADATA-TEMPLATE.csv`
- `config/targeted_capture_metadata.example.yaml`

## 11. End-of-day validation

```bash
find field-data -type f -name '*.wav' -ls
sha256sum field-data/**/*.wav > SHA256SUMS.txt
vcgencmd get_throttled
df -h .
```

Before processing, copy the campaign to a second storage device and verify:

- expected files and durations exist;
- center frequency and sample rate match the plan;
- gain and antenna settings stayed fixed;
- GPS coordinates are plausible;
- no undervoltage occurred.

## 12. Analysis principles for location work

Do not compare raw peak dBFS alone. Retain per site and frequency:

- average channel SNR;
- P95 channel SNR;
- local noise floor;
- occupancy;
- both repeat values;
- repeat median and difference;
- overload/passband warnings.

Use channel power relative to the local noise floor under fixed manual gain. Report a probable region, not an exact coordinate, unless directional or synchronized methods justify it.

## 13. Confirmed frequencies

```text
162.525000 MHz  CC8
162.587500 MHz  CC5
164.300000 MHz  CC7
164.325000 MHz  CC6
164.537500 MHz  CC8
164.725000 MHz  CC7
165.625000 MHz  CC6
167.137500 MHz  CC7
```

## 14. Safety and scope

This project is passive and receive-only.

- do not transmit or impersonate a network;
- do not trespass;
- do not operate equipment while driving;
- do not publish exact locations of sensitive infrastructure without careful validation and a legitimate reason;
- preserve uncertainty in every location estimate.
