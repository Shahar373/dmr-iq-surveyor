# Field procedure — multi-session P25 site geolocation

The authoritative checklist for a geolocation campaign. Read
[`docs/phase7-geolocation-design.md`](phase7-geolocation-design.md) first for what the results mean;
this document is how to produce them.

**Geolocation maturity: experimental.** The current P25/868 MHz workflow has not received in-band
ground-truth validation. Passing tests confirm the software runs correctly; they do not confirm RF
accuracy. See `docs/known-issues-v0.10.md` for what is and is not yet validated.

## 0. Once, before the first drive

```bash
cd ~/Projects/dmr-iq-surveyor
source .venv/bin/activate
python -m pip install -e '.[dev]'
pytest -q && ruff check .

cp config/sites/home.example.yaml config/sites/mobile.yaml   # fill in antenna/receiver/gain
cp config/p25_sites.example.csv   config/p25_sites.csv       # fill in your snapshot

dmr-surveyor geo import-sites config/p25_sites.csv --snapshot-id p25_sites_v1
dmr-surveyor geo sites
```

`geo sites` is the honest inventory of what can be attempted at all. Sites with no control-channel
frequency, and frequencies shared by two sites, are listed with a reason — they will never produce a
region, and knowing that before driving is the point.

## 1. Pick a gain and never change it

This is the single most consequential decision in the campaign. Levels recorded at different gain
settings are not comparable, and the whole method rests on comparing levels between places.

```bash
dmr-surveyor survey preflight runs/field/recordings \
  --band central_800_narrow --center-frequency 867406250 --sample-rate 5000000 --duration 120
```

Pick an IF gain reduction that leaves headroom at the strongest site you expect (a clipped or
compressed capture reports a *lower* SNR than a clean one, which the model reads as "further away").
A fast way to get a real number without trial and error: record ~10-15 s at a deliberately
insensitive setting (e.g. `--if-gr 55 --lna-state 8`, guaranteed not to clip), `dmr-surveyor inspect`
it, and read `aggregate.i.peak_abs` / `aggregate.q.peak_abs` from `sample_statistics.json` (values
are normalized, so `1.0` is full-scale). Headroom in dB is `-20*log10(peak_abs)`; reduce the IF gain
reduction by that minus a safety margin (10-15 dB) to land on a working value.

Write both `gain` (the IF gain reduction) **and** `lna_state` into `config/sites/mobile.yaml`:

```yaml
gain_mode: manual
gain: 26
lna_state: 8
```

`dmr-surveyor web serve` reads its default capture gain from **this file**, not from its own
`--if-gain-reduction`/`--lna-state` flags — those exist only as an override, and are never the place
to record a real value. The server prints which source it used (site profile, or a fallback) on
startup; a fallback is a mistake waiting to record a stop at the wrong gain, so treat it as one. A
site profile with no recorded gain is imported, but every measurement from it carries
`not_gain_comparable`.

## 2. Capture settings

```text
centre frequency: 867.406250 MHz     midpoint of the control channels in the snapshot
sample rate:      5.000 MS/s          covers 866.0-868.8 MHz in one capture
band profile:     central_800_narrow  matches what that capture can actually cover
duration:         90 s                18 analysis segments; 1.68 GiB
AGC:              off
antenna:          same mount, same height, same orientation at every stop
```

### Why 5 MS/s, and why not lower

The control channels span 866.0625-868.7500 MHz, or 2.6875 MHz. The SDRplay driver picks the
analog IF filter from the sample rate, and the steps are coarse: **every rate from 1.536 up to (but
not including) 5 MS/s gets the same 1.536 MHz filter**. So 3 and 4 MS/s cost storage bandwidth and
buy no extra spectrum, and 2 MS/s cannot cover the band at all.

| Rate | Analog IF | Usable | Covers 2.6875 MHz? | Per stop at 90 s |
|---|---|---|---|---|
| 2.0 MS/s | 1.536 MHz | 1.536 MHz | no | 0.67 GiB |
| 3.0-4.0 MS/s | 1.536 MHz | 1.536 MHz | no | 1.01-1.34 GiB |
| **5.0 MS/s** | **5.000 MHz** | **5.000 MHz** | **yes** | **1.68 GiB** |

## 2a. Storage: recordings are not kept

A campaign cannot keep its raw IQ. Twenty stops at 5 MS/s x 90 s is 33 GiB, and a Pi in the field
does not have it.

It does not need to. The recording is only required until the survey has extracted its observations
into SQLite; after that it is 1.68 GiB describing something already measured. So:

- Before every capture the free space is checked against what the capture needs plus headroom. A
  capture that does not fit is **refused**, with the numbers — filling the card mid-capture costs the
  stop and can leave the recording unreadable.
- After a survey **succeeds**, recordings beyond the newest `--keep-recordings` (default 1) are
  deleted, and each deletion is written to `recordings/retention.json`. A failed stop keeps its IQ
  regardless of this setting. The default of 1, rather than 0, is a transitional policy: no
  event-triggered IQ snippet mechanism exists yet, so keeping nothing would leave nothing to replay
  a field anomaly against. See `docs/known-issues-v0.10.md`.
- Every capture keeps its `*_capture_report.json` — a few kilobytes recording the settings, frame
  count, overflow count and timing of exactly what was recorded. A discarded recording leaves
  evidence, not a gap.

| Policy | Peak disk at 5 MS/s x 90 s |
|---|---|
| `--keep-recordings 0` | 1.68 GiB |
| `--keep-recordings 1` (default) | 3.35 GiB |
| keeping everything (not offered) | 33 GiB over 20 stops |

The app shows free space, the size of one stop and how many more stops fit; **Free disk** on the
Sites tab clears the kept recordings mid-campaign.

One capture measures every site's control channel at the same instant with the same receiver state.
That is why they are comparable at all — never retune between sites within a stop.

## 3. Choosing stops

Geometry decides the result far more than the number of stops does.

- **Surround, don't traverse.** Measurements strung along one road give a corridor-shaped posterior
  no matter how many there are. The solver reports this as `weak_geometry` when the detections span
  under 90° of azimuth around the estimate.
- **12–16 stops for a first campaign**, spread around the region rather than clustered.
- **Include stops where you expect to hear nothing.** A non-detection is real evidence and is often
  what closes a region — a campaign of detections only reports `unbounded_region`, correctly, because
  nothing bounds the site from the outside.
- **Vary distance, not just bearing.** Stops all at a similar range constrain the bearing poorly and
  can produce a ring-shaped region (which the map will draw as a polygon with a hole).
- **Avoid measuring from inside a structure, under a bridge, or beside a large metal object.** These
  produce outliers that the model reads as distance.
- **Prefer repeatable spots** you can return to; a second visit at a different time is one of the
  cheapest ways to tighten a region.

## 3a. Let the system pick the next stop

After each solve, `geo plan` — and the numbered markers in the field app — rank where a stop would
teach the most. The measure is how *unpredictable* a measurement there would be: somewhere a site is
certainly heard, or certainly not, teaches nothing about where it is.

Use it as a shortlist, not an instruction. It knows nothing about roads, access or safety, and a
suggestion 400 m into a field is a suggestion to stop at the nearest reachable point near it.
`dmr-surveyor geo export stops.gpx --format gpx` puts them into a phone navigator.

If the plan says every candidate has a predictable outcome, more stops of the same kind will not
help — change the geometry, or accept the regions you have.

## 4. At each stop

Serve the field app from the Pi and drive it from a phone on the same hotspot:

```bash
dmr-surveyor web serve --host 0.0.0.0 --token auto \
  --band central_800_narrow --site mobile --output runs/field
```

Then, at every stop:

1. Stop the vehicle, engine and any inverter off if they raise the noise floor.
2. Set your position — **Use phone GPS**, or tap the map. Give the stop a name. The readout shows
   how long ago it was marked; past 20 minutes the app asks you to confirm before recording, because
   a stop recorded against the *previous* stop's coordinates is the one mistake that silently
   corrupts a whole campaign.
3. Check the gain fields still show the campaign value.
4. Press **Record this stop** and watch it through capture → survey → measurements → solve.
5. Check the result: a new point should appear on the map, and the site list should update.

Browsers only expose GPS to pages served over HTTPS or from localhost, so over plain HTTP from a
phone the **Use phone GPS** button will refuse. Tapping the map is the intended fallback and is
accurate enough — the model's shadow-fading term dwarfs a 20 m positioning error.

The same work is available without the app:

```bash
dmr-surveyor survey capture runs/field/recordings \
  --band central_800_narrow --site mobile --site-id <stop> \
  --survey-output runs/field/<stop> --run-id <stop> \
  --center-frequency 867406250 --sample-rate 5000000 --duration 120 \
  --if-gr <campaign value> --latitude <lat> --longitude <lon>
dmr-surveyor geo measurements
dmr-surveyor geo solve --output runs/field
```

## 5. Reading the result

```bash
dmr-surveyor geo sites
dmr-surveyor geo history BEE00:37D:1:30
```

| Status | What to do about it |
|---|---|
| `ok` | A bounded region. Read the 90% area, not the mode. |
| `insufficient_evidence` | Fewer than three detections. Add stops closer to where it is heard. |
| `unbounded_region` | Nothing bounds it from outside. Add stops where you expect *not* to hear it. |
| `weak_geometry` | All detections from one bearing. Add stops on the far side. |
| `frequency_unknown` | No control channel on record. Nothing to measure until one is found. |
| `no_measurements` | Every measurement was excluded — usually a shared frequency. |

Individual measurements can also be set aside, always with a reason:

| Exclusion | Meaning |
|---|---|
| `not_covered` | Outside the run's measured usable passband. We did not look; not evidence. |
| `level_unreliable` | Detected outside the measured passband, so the roll-off understates its level. |
| `receiver_artifact` | Landed on the receiver's own DC/LO spike, whose level is a property of the radio. |
| `superseded_channel` | The site has two control channels; only one may count per stop. |
| `run_excluded` | The whole stop was barred — a truncated capture or driver overflows. |
| `ambiguous` | The frequency is on record for more than one site. |

A stop whose capture was truncated, or which suffered driver overflows, is **excluded automatically**.
A signal that was there but was not recorded long enough to be detected would otherwise arrive as a
non-detection, and a non-detection is evidence that pushes the site away from that stop — a confident
wrong measurement rather than a missing one.

The gain actually applied is stored per stop. If one stop was recorded at a different gain from the
rest of the campaign, every measurement from it is flagged `gain_differs_from_campaign`.

If a stop went wrong — you stood in the wrong place, the antenna was knocked, you forgot to update
the position — the **Stops** tab sets it aside without losing it: its measurements stop counting and
the reason is recorded. Delete only a stop that should never have existed.

`geo history` is the campaign's progress report: the 90% area for a site should shrink as sessions
accumulate. If it stops shrinking, more stops of the same kind will not help — change the geometry.

### What to expect

The figures below come from a **simulation** on this repository's own solver, not from measurements:
26 sites scattered over a 30 km metro area, base-station reference levels of 45–60 dB above the noise
floor at 1 km, path-loss exponents of 3.0–4.0, and this project's default 8 dB shadow-fading term.
They calibrate expectations about campaign size; they are not a promise about your system.

| Campaign | Sites with a bounded region | Median mode error | Median 90% region |
|---|---:|---:|---:|
| 20 stops, clustered within ~15 km | 14 of 20 | 365 m | 246 km² |
| 20 stops, spread over ~35 km | 16 of 20 | 739 m | 163 km² |
| 30 stops, spread over ~35 km | 18 of 20 | 583 m | 72 km² |
| 20 stops, repeated twice | 18 of 20 | 814 m | 56 km² |

Three things this says, all of which match the method's known limits:

- **The mode lands within a few hundred metres well before the region gets small.** Do not read the
  mode as an answer the region does not support; that gap is exactly what the region is reporting.
- **Spread matters more than count for closing regions, count matters more for the mode.** Clustered
  stops give a good mode and a huge region; spread stops give more bounded regions.
- **Regions stay large in absolute terms.** Reducing a 900 km² metro to a 56 km² region is a
  sixteen-fold search-area reduction and a genuinely useful result. It is not a tower coordinate, and
  no number of omnidirectional RSSI stops will make it one — that needs directional bearings.

### Solve cost

The in-field solve after each stop runs at a coarser grid (`--solve-resolution-m`, default 250 m) so
it finishes in seconds rather than minutes. Run the full-resolution pass once at the end of the day:

```bash
dmr-surveyor geo solve --output runs/geo --resolution-m 100
```

On a long campaign, `dmr-surveyor web serve --no-solve-after-capture` skips the in-field solve
entirely and records stops as fast as you can drive between them.

## 6. What will not work, and why

- **A frequency used by two sites cannot be attributed.** It is excluded, not guessed. Resolving it
  needs control-channel decoder evidence (RFSS/Site from the RFSS Status Broadcast), which this phase
  does not implement.
- **A simulcast site has no single position.** If a region is large and the residuals are large and
  unstructured, suspect several transmitters keyed together; the estimator fits one and says so via
  `source_model: single_transmitter_assumed`.
- **A hilltop transmitter with line of sight measures stronger than a near one behind a building.**
  The model has no terrain. Treat a region that disagrees with the local topography with suspicion,
  and look at the per-measurement residuals in the report before believing it.
- **The mode is not a coordinate.** Report the region. Do not drive to the mode expecting a mast.

## 7. Scope

Passive and receive-only throughout. Nothing here transmits, injects, impersonates or decrypts.
Do not enter restricted property to reach a stop, and do not publish a region as a confirmed
location of infrastructure.
