# DMR IQ Surveyor — field recording guide

This document is the operational checklist for future field sessions. Read it before leaving home and keep a copy available offline.

## 1. Does Phase 5 require a new recording?

No. Phase 5 can be run and validated entirely from the existing Phase 4.1 decode tree. It converts the selected DSD-FME logs into persistent events, sessions and channel inventory.

A new recording is needed only when the objective is one of the following:

- recover Talkgroup or Radio IDs that were absent from the short source captures;
- observe more voice, data or control activity;
- compare channel strength at different locations;
- estimate a transmitter coverage area or location;
- verify IQ/QI orientation against a known carrier;
- improve a degraded channel by recording it nearer the receiver passband center.

## 2. Separate the session objective before recording

Do not mix all objectives into one uncontrolled capture. Choose one of these modes.

### Mode A — multi-location survey

Purpose: compare all eight confirmed DMR carriers between sites and build a coverage/geolocation dataset.

Use short, repeatable wideband recordings with identical receiver and antenna settings at every site.

Recommended initial profile:

```text
center frequency: 164.831250 MHz
sample rate:      10.000 MS/s
format:           signed 16-bit complex IQ
capture length:   15–20 seconds
repeats:          2 per site
sites:            8–12 for the first campaign
```

The center frequency is approximately the midpoint of the confirmed range, 162.525000–167.137500 MHz. It keeps all confirmed channels well inside a 10 MHz recording and away from DC.

At 10 MS/s signed complex int16, storage is approximately 40 MB/s:

```text
15 seconds ≈ 600 MB
20 seconds ≈ 800 MB
2 × 20 seconds ≈ 1.6 GB per site
10 sites ≈ 16 GB
```

This profile remains compatible with the existing 10 MS/s Phase 4 extraction design.

### Mode B — targeted identity capture

Purpose: recover Talkgroup and Radio IDs from one known channel during active traffic.

Start with:

```text
164.537500 MHz
Color Code 8
```

This channel already showed coherent Group Voice and complete VC1–VC6 activity.

Do not use the existing 10 MS/s extraction profile unchanged on a 250 or 500 kS/s file. Phase 5.1 / Issue #13 must provide a validated targeted-rate preset first.

After Phase 5.1 is implemented, the intended profile is:

```text
center exactly on the channel
sample rate: 250 or 500 kS/s
capture length: 5–15 minutes
record during known activity
```

### Mode C — directional bearing survey

Purpose: estimate transmitter bearing from several sites.

Use a directional VHF antenna, fixed manual gain and a reproducible rotation procedure. See `docs/TRANSMITTER-LOCATION-STUDY.md`.

## 3. Equipment checklist

Required:

- Raspberry Pi 5 and storage with sufficient free space;
- SDRplay RSP1B-class receiver;
- the same VHF antenna for the entire campaign;
- the same coax cable and adapters for every site;
- stable USB-C PD power or UPS HAT;
- phone with GPS and camera;
- tripod or repeatable vehicle-roof mount;
- measuring tape or marked mast for repeatable antenna height;
- notebook or the metadata template below.

Recommended:

- second power bank reserved only for the Pi/SDR;
- 50-ohm terminator for receiver-noise checks at the beginning or end of the campaign;
- external USB storage for immediate backup;
- compass;
- weatherproof cover that does not enclose or overheat the Pi;
- for later bearing work: 3-element Yagi or log-periodic antenna and a step attenuator.

## 4. Receiver settings that must stay fixed

For strength comparison, changing gain between sites invalidates the simple comparison.

Record and keep constant:

- AGC: off;
- LNA state;
- IF gain reduction;
- RF bandwidth;
- sample rate;
- center frequency;
- antenna;
- coax and adapters;
- antenna height;
- antenna orientation;
- IQ format;
- SDRconnect version;
- any notch filters, preamplifiers or bias-T state.

Choose manual gain at the first site so the strongest confirmed carrier has comfortable headroom and does not overload or clip. Do not optimize gain independently at every location.

If overload occurs at a later site, make a separate clearly labelled low-gain repeat. Do not silently replace the standard-gain capture.

## 5. Raspberry Pi checks before leaving

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

Interpret `vcgencmd get_throttled`:

```text
throttled=0x0
```

is the desired result. Any current undervoltage or throttling flag means the power problem should be corrected before recording.

Do not run spectrum analysis or decoding in the field while the Pi is on unstable power. Capture first; process later on stable power.

## 6. Site selection for the first multi-location campaign

Use 8–12 sites that surround the suspected coverage area instead of following one road or one straight line.

Include:

- at least one high-elevation open site;
- at least one low urban site;
- sites north, south, east and west of the suspected area;
- at least two sites expected to be weak;
- at least two sites expected to be strong;
- no two sites with nearly identical geometry unless they are deliberate repeats.

Initial spacing can be 1–3 km. After a first heatmap identifies a likely high-strength region, use a second campaign with 200–500 m spacing around that region.

Avoid:

- recording inside a vehicle;
- standing next to large metal structures;
- placing the antenna directly beside the Pi, screen, power bank or USB cable bundle;
- sites where the antenna height or mount cannot be reproduced;
- private or restricted property without permission.

## 7. Per-site procedure

Use the same sequence at every site.

1. Park or stand in a safe, legal position.
2. Record GPS latitude, longitude, altitude and reported accuracy.
3. Photograph the antenna placement and surrounding horizon.
4. Install the antenna at the standard height.
5. Keep antenna polarization and orientation identical to previous sites.
6. Start the Pi and SDR, then allow at least 60 seconds for thermal and gain stabilization.
7. Confirm manual gain and all receiver settings.
8. Check power and storage:

   ```bash
   vcgencmd get_throttled
   df -h .
   ```

9. Record the first 15–20 second wideband IQ file.
10. Wait approximately 30–60 seconds.
11. Record an identical second file.
12. Verify that both files exist and have plausible size.
13. Record any local interference, nearby transmitters, buildings, terrain or moving vehicles.
14. Recheck `vcgencmd get_throttled` before leaving the site.

Two captures are important. They reveal whether one measurement was distorted by fading, temporary traffic or a local interferer.

## 8. File and directory naming

Recommended structure:

```text
field-data/
└── 202607XX_geolocation_campaign_01/
    ├── session.md
    ├── sites.csv
    ├── site_01/
    │   ├── site_01_repeat_01_164831250HZ_10000000SPS.wav
    │   ├── site_01_repeat_02_164831250HZ_10000000SPS.wav
    │   └── photos/
    ├── site_02/
    └── ...
```

Do not rename away the center-frequency suffix expected by the existing filename fallback unless the container metadata reliably contains the center frequency.

Use one run ID per campaign, for example:

```text
202607XX_geolocation_campaign_01
```

Use a new run ID for every later campaign so Phase 5 can accumulate observations rather than replace the previous run.

## 9. Metadata required for every site

Record at minimum:

```text
campaign_id
site_id
repeat_id
local_datetime
utc_datetime
latitude
longitude
altitude_m
gps_accuracy_m
center_frequency_hz
sample_rate_hz
capture_duration_s
receiver_model
receiver_serial
sdrconnect_version
agc_enabled
lna_state
if_gain_reduction_db
rf_bandwidth_hz
antenna_model
antenna_height_m
antenna_orientation_deg
polarization
coax_type
coax_length_m
filters_or_lna
power_source
vcgencmd_before
vcgencmd_after
weather
terrain
nearby_obstacles
notes
file_path
sha256
```

A CSV template is provided in `docs/FIELD-SESSION-METADATA-TEMPLATE.csv`.

## 10. End-of-day validation

Before deleting or moving anything:

```bash
find field-data/202607XX_geolocation_campaign_01 -type f -name '*.wav' -ls
sha256sum field-data/202607XX_geolocation_campaign_01/site_*/*.wav \
  > field-data/202607XX_geolocation_campaign_01/SHA256SUMS.txt
vcgencmd get_throttled
df -h .
```

Copy the campaign to a second storage device before processing.

Check:

- two recordings exist per site;
- durations and file sizes are consistent;
- center frequency and sample rate are identical;
- metadata rows match the file names;
- gain and antenna settings did not change;
- GPS coordinates are plausible;
- no undervoltage occurred.

## 11. Analysis principles for location work

Do not compare raw peak dBFS alone.

For each confirmed frequency and site, retain:

- average channel SNR;
- P95 channel SNR;
- local noise floor;
- occupancy;
- both repeat values;
- median of repeats;
- difference between repeats as an uncertainty indicator.

The primary comparison should be channel power relative to the local noise floor under fixed manual gain.

A strong value at one site is not proof that the transmitter is physically close. Terrain, antenna pattern, multipath, buildings and line of sight can dominate the result.

## 12. Confirmed frequencies for the first campaign

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

One 10 MHz recording centered at 164.831250 MHz can capture all eight simultaneously.

## 13. Field safety and project scope

This project is passive and receive-only.

- do not transmit;
- do not attempt to join or impersonate a network;
- do not trespass;
- do not obstruct roads or operate equipment while driving;
- do not publish exact locations of sensitive infrastructure without careful validation and a legitimate reason;
- preserve uncertainty in all location estimates.
