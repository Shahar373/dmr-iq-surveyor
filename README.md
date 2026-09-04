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
- Phase 5.2: exact 5m and 62k5 profiles for additional SDRconnect recording modes
- Phase 6A: protocol-agnostic RF survey (discovery, persistent inventory, run comparison), the first step toward multi-protocol support (P25 in 866-870 MHz)
- Phase 7: multi-session P25 site geolocation (reference registry, censored-likelihood grid posterior, credible-region polygons) and a field web app
- Phase 7.1: live (moving) survey — measure while driving, write no IQ, one virtual stop per 150 m

## Project documentation

- [`docs/development-history.md`](docs/development-history.md)
- [`docs/phase4-design.md`](docs/phase4-design.md)
- [`docs/phase5-design.md`](docs/phase5-design.md)
- [`docs/phase5-session-semantics.md`](docs/phase5-session-semantics.md)
- [`docs/PHASE5-1-TARGETED-CAPTURE.md`](docs/PHASE5-1-TARGETED-CAPTURE.md)
- [`docs/PHASE5-2-ADDITIONAL-RATES.md`](docs/PHASE5-2-ADDITIONAL-RATES.md)
- [`docs/phase6-design.md`](docs/phase6-design.md)
- [`docs/phase6a-survey.md`](docs/phase6a-survey.md)
- [`docs/phase7-geolocation-design.md`](docs/phase7-geolocation-design.md)
- [`docs/PHASE7-FIELD-GEOLOCATION.md`](docs/PHASE7-FIELD-GEOLOCATION.md)
- [`docs/PHASE6-FIELD-800MHZ.md`](docs/PHASE6-FIELD-800MHZ.md)
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
  -> 50, 62.5 or 100 kHz complex baseband
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

## Phase 5.1 and 5.2 — targeted known-frequency capture

Supported exact-rate profiles:

| Profile | Input rate | Intermediate rate |
|---|---:|---:|
| `10m` | 10,000,000 S/s | 100,000 S/s |
| `5m` | 5,000,000 S/s | 100,000 S/s |
| `500k` | 500,000 S/s | 100,000 S/s |
| `250k` | 250,000 S/s | 50,000 S/s |
| `62k5` | 62,500 S/s | 62,500 S/s |
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

See [`docs/PHASE5-1-TARGETED-CAPTURE.md`](docs/PHASE5-1-TARGETED-CAPTURE.md) and [`docs/PHASE5-2-ADDITIONAL-RATES.md`](docs/PHASE5-2-ADDITIONAL-RATES.md).

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

The `500k` profile remains the preferred first long capture. `250k` is supported, while `62k5` is intended for already-created narrow SDRconnect IQ files with limited tuning margin. `5m` supports short wideband candidate extraction.

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

## Phase 6A — protocol-agnostic RF survey

```bash
dmr-surveyor survey run recording.wav --band central_800 --site home
dmr-surveyor survey list
dmr-surveyor survey show RUN_ID
dmr-surveyor survey compare RUN_A RUN_B --band central_800
```

Given one wideband IQ recording, Phase 6A discovers active RF signals with no prior frequency list, using bounded time-segmented analysis so runtime and memory stay predictable on long captures. Every observation stores relative `dBFS/Hz` power (never dBm without calibration), an honestly separated `occupancy_pct` (fraction of analyzed time busy) and `persistence` (fraction of segments independently detected), and a measured usable passband rather than an assumed Nyquist width. No protocol decoder runs in Phase 6A: `classification` is always `unknown`; `spectral_class` is a spectral-shape hypothesis, never a protocol confirmation.

Runs and observations persist in the same SQLite database as the DMR inventory (`runs/inventory/dmr_inventory.sqlite3` by default), extended additively — existing tables are untouched. `survey compare` works with no protocol decoder installed and reports `NEW`, `MISSING_THIS_RUN`, `STABLE`, `SNR_CHANGE`, `OCCUPANCY_CHANGE`, `PERSISTENCE_CHANGE`, or `NOT_COMPARABLE` between two runs.

Band profiles (`config/bands/*.yaml`, e.g. `central_800.yaml` for 866-870 MHz, `central_800_recon.yaml` for a short first-look capture) describe where to look; site profiles (`config/sites/*.yaml`, copy `home.example.yaml`) record the fixed measurement context. See [`docs/phase6a-survey.md`](docs/phase6a-survey.md) for the full design, schema and acceptance criteria, [`docs/phase6-design.md`](docs/phase6-design.md) for the overall Phase 6 roadmap toward P25, and [`docs/PHASE6-FIELD-800MHZ.md`](docs/PHASE6-FIELD-800MHZ.md) for a field-ready capture procedure at a new site.

## Phase 7 — P25 site geolocation

```bash
dmr-surveyor geo import-sites config/p25_sites.csv --snapshot-id p25_sites_v1
dmr-surveyor geo sites
dmr-surveyor geo measurements
dmr-surveyor geo solve --output runs/geo
dmr-surveyor geo history BEE00:37D:1:30
dmr-surveyor geo export runs/geo/map.geojson
```

Phase 7 turns the Phase 6A observation inventory into per-site transmitter *location estimates*: a
posterior probability surface and credible-region polygons for each P25 site, built from several
passive recording sessions made at different places and improving as sessions accumulate.

### Site attribution is explicit, never assumed

A received level measured on a frequency is not a measurement of a site. Every measurement stores how
it was attributed:

| `attribution` | Meaning | Used by the solver |
|---|---|---|
| `decoded` | RFSS/Site read from control-channel decoder evidence | yes (reserved; nothing emits it yet) |
| `inferred_unique` | measured, and exactly one registry site uses that frequency | yes, flagged |
| `ambiguous_reuse` | measured, but more than one site uses that frequency | no — excluded with a reason |
| `frequency_unknown` | site is known, no control-channel frequency on record | no — nothing to measure |

and separately, whether it can be used at all:

| `usability` | Meaning |
|---|---|
| `usable` | inside the run's *measured* usable passband, and the run has a position |
| `not_covered` | outside the measured passband. **Not evidence** — we did not look there |
| `level_unreliable` | detected outside the measured passband, so the roll-off understates its level |
| `receiver_artifact` | landed on the receiver's own DC/LO spike, whose level is a property of the radio |
| `superseded_channel` | the site has two control channels; only one may count per stop |
| `run_excluded` | the whole stop was barred — a truncated capture, driver overflows, or set aside by the operator |
| `no_position` | the run has no coordinates |
| `ambiguous` | excluded by the ladder above |

A NAC is not a site identifier — one NAC is routinely shared by many sites in a system — so it is
stored as context and never used to attribute a measurement.

### Detections and non-detections are both evidence

A frequency that was inside the measured passband and produced nothing is a **left-censored**
measurement, not a missing one, and is often what closes a region. A campaign made entirely of
detections is reported `unbounded_region`, correctly.

### The estimator

For each site, a grid posterior over position with the log-distance model
`mu = P0 - 10 n log10(d/d0)`, Gaussian shadow fading, and `P0`/`n` marginalised out. Detections use a
Gaussian likelihood; non-detections use `Phi((y_threshold - mu)/sigma)`. Evaluation is a bounded
coarse pass over the whole region followed by a fine pass restricted to where the mass is, chunked
over cells so peak memory does not scale with the grid. Credible regions are highest-density regions,
emitted as GeoJSON MultiPolygons with holes where the posterior is annular.

The solver refuses rather than guessing:

| `status` | Raised when |
|---|---|
| `ok` | enough evidence and geometry for a bounded region |
| `insufficient_evidence` | fewer than `--min-detections` (default 3) usable detections |
| `unbounded_region` | the 90% region reaches the edge of the analysed area |
| `weak_geometry` | detections span under 90 degrees of azimuth around the estimate |

A region is a search-area reduction, not a transmitter coordinate, and reports never present the
posterior mode as one. Simulcast — several transmitters keyed together as one logical site — is not
modelled; every solution records `source_model: single_transmitter_assumed`.

### Where to go next

```bash
dmr-surveyor geo plan
```

Geometry decides a campaign more than stop count does, and choosing well is hard from inside a car.
After every solve the system ranks candidate places by how much a stop there would teach: for each
site, the binary entropy of the detection probability its current posterior predicts, weighted so a
site already pinned down stops pulling the plan, and damped near places already measured.

A place where a site is certainly heard, or certainly not, teaches nothing about where it is. A place
where the posterior genuinely cannot say teaches the most. The field app draws this as a layer and
numbers the top suggestions; `geo export plan.gpx --format gpx` loads them into a phone navigator.

It is a planning aid computed from current beliefs, not a prediction about the transmitters.

### Keeping stops comparable

The method compares levels between places, so anything that shifts one stop's levels as a whole
corrupts it. Three guards, all reported whether or not they fire:

- **Per-stop common-mode offset.** If every site heard at one stop sits the same distance from its
  predicted level while the rest of the campaign fits, the stop is what differs — a re-seated
  antenna, local interference, front-end compression. The solve runs a second pass with that offset
  removed. It is only estimated where it is identifiable (three or more sites heard), only applied
  when the sites actually agree on it (a large but scattered residual is model misfit, not a shared
  shift), and the reported magnitude is a lower bound because the first pass already absorbed part
  of it. `--no-common-mode` reports without applying.
- **Gain drift.** The gain actually applied is stored per stop, and measurements from a stop
  recorded at a gain other than the campaign's are flagged.
- **Noise-floor shift.** Levels are SNR above the local noise floor, so a floor that moves takes
  every level with it. A stop more than 4 dB from the campaign's median floor is flagged.

### Managing stops and exporting

The field app's **Stops** tab lists every stop and can set one aside — its measurements stop
counting, the reason is recorded, and it can be put back — or delete it outright. Exports:

```bash
dmr-surveyor geo export survey.kml --format kml   # regions over imagery in Google Earth
dmr-surveyor geo export stops.gpx --format gpx    # suggested next stops for a navigator
dmr-surveyor geo export map.geojson               # everything, for anything else
```


### Field web app

```bash
dmr-surveyor web serve --host 0.0.0.0 --token auto \
  --band central_800_narrow --site mobile --output runs/field
```

A local control surface served by the Pi and opened from a phone on the same hotspot: mark your
position (phone GPS or a map tap), record a stop with one button, watch capture -> survey ->
measurements -> solve progress live, and see the measurement points and credible regions on the map.
Built on the standard library's HTTP server with a dependency-free single-page app — a field tool
must not fail because a dependency did not install.

It binds to loopback unless `--host` says otherwise, because the API can start a capture; on an open
network pass `--token auto` and use the printed URL.

Browsers gate `navigator.geolocation` behind a *secure context*: over plain HTTP a phone does not
merely warn, it refuses to share GPS at all. Pass `--tls` and the app issues a self-signed
certificate covering loopback and the Pi's own addresses, prints its SHA-256 fingerprint, and serves
over HTTPS. Accept the warning once per device ("Advanced" -> "Proceed"), or install the certificate
to trust it permanently — it is issued for 397 days precisely so a phone will accept it as trusted.
The pair lives in `<output>/tls` and is reused, not reissued, so a phone that has trusted it stays
trusted; `--tls-cert/--tls-key` take your own instead. Without TLS you can still tap the map to place
a position, but a **live drive cannot run**.

The solve that runs after each stop uses a coarser grid (`--solve-resolution-m`, default 250 m) so it
finishes in seconds; `--no-solve-after-capture` skips it entirely on a long campaign. Run
`dmr-surveyor geo solve --resolution-m 100` once at the end of the day.

**Recordings are not kept.** A 5 MS/s, 90 s stop writes 1.68 GiB, so a 20-stop campaign would be
33 GiB — more than a Pi in the field has. The recording is only needed until the survey has
extracted its observations into SQLite, so free space is checked *before* every capture (a capture
that does not fit is refused, with the numbers) and, after a survey succeeds, recordings beyond
`--keep-recordings` (default 1) are deleted, with each deletion written to a ledger and every
capture's `*_capture_report.json` left behind. A failed stop keeps its IQ. Peak disk is 3.35 GiB.

### Live (moving) survey

A stationary stop records IQ; a drive does not record anything at all. The receiver streams
continuously, each second of samples is reduced to a spectrum and tagged with where the phone said
the car was, and the samples are dropped. Every 150 m square of road becomes one ordinary
`survey_runs` row with its observations — about **8 KiB, measured** — so a 13 km drive costs under a
megabyte and a whole campaign of driving costs single-digit megabytes. That is the reason the mode
exists on a Pi that does not have gigabytes to spare.

Because a bin is written as an ordinary survey run, `geo measurements`, `geo solve`, the stop list,
the exclusions, the common-mode check and the next-stop planner consume a drive without knowing one
happened.

Open the app over HTTPS on the phone, go to the **Drive** tab, tap *Share my location*, then *Start
drive*. Bins appear on the map as they are written and the credible regions are re-solved in the
background every few bins, so the polygons shrink while you are still driving. Stopping the drive
runs a final solve.

Two length scales decide the design, and neither is adjustable by taste:

- **A window is one second.** At 50 km/h that is 14 m, about 40 wavelengths at 868 MHz — the
  drive-test convention for averaging fast (multipath) fading out of a level to recover the local
  mean, which is the quantity the path-loss model is written in.
- **A bin is 150 m.** Shadow fading decorrelates over roughly 10–50 m in a city and 100–200 m in
  suburbs. Feeding the solver a measurement per second would treat a thousand correlated samples as
  a thousand independent constraints and shrink a region by a factor near 240, almost all of it
  fabricated. A bin is measured once; driving the same street again lands on the same id and
  replaces it rather than adding near-identical evidence beside it.

Sampling is therefore **time-triggered, placement is distance-triggered**. Standing still is bounded
too: a bin closes after `live_max_windows_per_bin` windows (10) instead of accumulating spectra for
as long as the car sits there, so a red light costs ~17 MB rather than 100 MB a minute, and windows
after that are dropped before the FFT.

Without a phone — for a stationary measurement that writes no IQ:

```bash
dmr-surveyor live stop --latitude 32.0500 --longitude 34.7900 --seconds 30 \
  --band central_800_narrow --site mobile
```

A 30 s live stop replaces a 560 MiB recording with an 8 KiB row. What is lost is the recording
itself: a live measurement cannot be re-analysed later with different settings, because the samples
are gone. That is the trade, and it is the right one when the alternative is not measuring at all
for want of a card.

See [`docs/phase7-geolocation-design.md`](docs/phase7-geolocation-design.md) for the full design and
schema, [`docs/PHASE7-FIELD-GEOLOCATION.md`](docs/PHASE7-FIELD-GEOLOCATION.md) for the campaign
procedure, and [`config/p25_sites.example.md`](config/p25_sites.example.md) for the snapshot format.


## Result packaging

Generated runs, reports, metadata and the persistent SQLite database can be archived without including raw IQ files:

```bash
python3 scripts/package_results.py \
  --output decoded_results.zip \
  runs/targeted/my_run \
  runs/inventory/dmr_inventory.sqlite3 \
  config/my_capture.yaml
```

## Tests

```bash
pytest -q
ruff check .
```

The suite covers metadata parsing, spectrum processing, candidate detection, streamed DSP, 10m/5m/500k/250k/62k5 profiles, off-center frequency mixing, peak-safe WAV output, DSD-FME quality parsing, polarity selection, active slots, event parsing, session semantics, capture metadata migration, standalone-log import, idempotent SQLite import and cross-run aggregation, plus Phase 6A band/site profiles, segmented discovery, occupancy vs. persistence, usable-passband measurement, idempotent survey import with capture-time-based history, and protocol-agnostic run comparison, plus Phase 7 reference-snapshot parsing and idempotent import, the measurement attribution and usability ladder, projection/geometry, the grid posterior and its refusal statuses, credible-region contours including annuli and separated modes, the geolocation CLI, and the field web app's routing, authorisation and job lifecycle — all against synthetic fixtures generated at test time, no real IQ data is committed. An optional real-recording integration test is documented in [`docs/phase6a-survey.md`](docs/phase6a-survey.md).

## Passive scope

The project performs receive-side offline analysis only. It contains no transmit, injection, impersonation, authentication bypass, brute-force or decryption capability.
