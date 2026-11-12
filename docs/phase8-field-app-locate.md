# Phase 8 field app: "Locate" (design)

Status: **approved 2026-11-12. PR 1 (section 4.1–4.5) implemented; PRs 2–4 to follow.**
Mockup: [`mockups/field-app-locate.html`](mockups/field-app-locate.html), also published at
<https://claude.ai/artifact/7yU3JPrZwgGRu1M1Mi5oBX> (private to the owner). Open it in a phone
browser; every screen in section 3 can be clicked through.

This document is the design for the field app's Phase 8 workflow. It sits beside
[`phase8-fixed-emitter-geolocation.md`](phase8-fixed-emitter-geolocation.md), which stays
authoritative for the estimator. The UI work was explicitly requested by the owner on
2026-11-11. That request is what `CLAUDE.md` means by "the milestone that calls for it".

## 1. Goal

**Type a frequency, press Start, drive.** Everything else is optional and sits behind
*Advanced*. Today the field app is built around P25 sites. Locating one carrier means
importing a placeholder P25 row, tuning by hand, calibrating, driving, and then solving from
the command line (`FIELD-DRIVE-USB-GPS-AND-GAIN.md`). The new screen does the same work in
one flow and solves the Phase 8 way (`geo emitter solve`).

### Decisions made with the owner (2026-11-11)

| question | decision |
|---|---|
| language | Hebrew, right-to-left. Units and identifiers (MHz, dB, SNR) stay Latin |
| tuning | automatic from the target frequency, with an override under Advanced |
| targets per drive | one for now, with a structure that allows several later |
| existing features (IQ stops, P25 Sites/Stops, KML/GPX) | kept, one tap away under *Advanced* |
| gain calibration | automatic on the first Start of a new target, then fixed |
| region while driving | **only on request** (a button), never automatic |
| campaigns | one campaign per target frequency, continued across days; "start over" opens a new one |
| "where to drive next" | yes, simple: highlighted areas plus one spoken sentence, refreshed with the region |
| screen | phone or tablet, portrait or landscape: one responsive layout |
| stationary 60 s measurement | a button on the drive screen |
| known position (validation) | yes, under Advanced; hidden while driving, scored at the end |
| process | this document and a mockup first, then small PRs |

## 2. Principles this design keeps

- **The web app stays a thin layer over the spine.** Every server action calls something that
  already exists or is added to `geo/`, `live/` or `survey/` with its own tests:
  `upsert_emitter`, `run_gain_calibration`, `LiveSession`, `solve_emitters`, `plan_next_stops`.
  No estimator logic lives in `web/`.
- **Discovery before reference.** The target frequency chooses *where the receiver is tuned*,
  which any capture has to choose. It does not change what the detector looks for. The
  detector scans the whole generated window (section 4.3) with the same thresholds as every
  narrowband profile. The target is matched to observations afterwards, by
  `materialise_emitter_measurements`, exactly as from the command line.
- **A region is a search area, not a coordinate.** The result screen never shows a single pin
  as "the transmitter". The most likely cell is drawn as a small dot *inside* the region and
  labelled as such.
- **Backward compatibility.** No CLI command, table or label changes. The current app moves
  to `/classic` unchanged and is what *Advanced* opens. Schema changes are additive only.

## 3. Screens

Hebrew strings are the proposed copy; the mockup shows them in place.

### 3.1 Home: "מה מאתרים?" (what are we locating?)

- One large numeric field, `MHz`, e.g. `469.350`. The keyboard is numeric on a phone.
- Under it, the **recent targets** as chips: frequency, label, number of drives, region size
  if solved. A tap fills the field and resumes that target's campaign.
- One primary button: **התחל** (Start).
- Status row: SDR ✓/✗, GPS (USB GPS / phone / none), disk. A red item disables Start and
  says why in one line ("אין GPS: חבר את ה-USB GPS או שתף מיקום מהטלפון").
- Menu (☰): *Advanced*, *Settings*, *Classic app*.

Start does, in order: declare the emitter if new (4.1), choose the tuning (4.2), select or
open the target's campaign (4.4), calibrate gain if this campaign has none yet (3.2), and
start the drive (3.3).

### 3.2 Calibration: "עמוד במקום, דקה אחת" (stay still, one minute)

Shown only on the first Start of a target, or after "start over". A progress ring, a voice
prompt in Hebrew, a Cancel button. At the end: "ההגבר נקבע. אפשר לנסוע." and the drive starts
by itself. If the target was not heard while parked, the screen says so and offers *Drive
anyway* (noise-floor choice) or *Move and retry*. This is the same decision the backend makes
today, now put to the operator.

### 3.3 Drive

The map fills the screen and follows the car. Over it, a compact HUD with three numbers that
can be read at a glance:

| cell | content |
|---|---|
| last measurement | **נשמע · 14 dB** (green) / **לא נשמע** (grey), from the target only |
| measurements | count of bins this drive, and total in the campaign |
| GPS | USB GPS / phone / **lost** (red) |

The map shows the drive bins coloured by the target's level, grey where it was looked for
and not heard, and the road already covered.

Three buttons, large, bottom of the screen (on the side in landscape):

- **עצור** (Stop): ends the drive. Every bin is already stored.
- **מדוד כאן 60 ש׳** (measure here 60 s): the existing hold. It pulses when the last bin
  had the target just under the detection gate ("שווה לעצור כאן").
- **עדכן תוצאה** (update result): solve now (3.4). Disabled while a solve runs. The previous
  region stays visible, marked with its age ("לפני 6 דק׳").

Voice, in Hebrew, off with one toggle: drive started, GPS lost/back, every 5th bin, target
heard after a silence or lost after hearing it ("נשמע שוב", "אבד"), worth a stop, solve done.

### 3.4 Result (on request, and at Stop)

- The 50 % and 90 % regions, with the most likely cell as a small dot.
- One status line in words, from the solver's own status:
  - `ok` → "אזור חיפוש: 3.2 קמ״ר"
  - `unbounded_region` → "האזור פתוח לכיוון ... ; כדאי לנסוע גם מעבר לו"
  - `weak_geometry` → "נשמע מכיוון אחד בלבד"
  - `insufficient_evidence` → "עוד אין מספיק מדידות"
- **Where to go next**: up to three highlighted areas and one spoken sentence that names a
  compass direction relative to the region ("כדאי למדוד גם מצפון-מזרח לאזור").
- **Suspect measurements**, if any: marked on the map with "כדאי למדוד שוב כאן".
- With a known position on record: the error and whether it is inside the 90 % region.
- Actions: *continue driving*, *share* (GeoJSON / KML file), *start over* (new campaign,
  new calibration).

### 3.5 Advanced (per target, collapsed by default)

Label; emission (continuous / intermittent, with the one-line consequence); tolerance;
centre frequency and sample rate (auto values shown, editable before the first measurement
of a campaign only); gain (read-only after calibration); shadowing correlation (500 m,
"1000 בשטח פתוח"); known position for validation (lat/lon or tap on map, and its source).

## 4. What the server needs

Each item names the existing function it composes. New endpoints sit beside the old ones;
none of the old ones changes.

### 4.1 Target

`POST /api/locate/target {frequency_mhz, label?, emission?, tolerance_hz?, truth?}` →
`upsert_emitter`. Key derived from the frequency, `f469_350000`, so the same frequency always
resumes the same target. `GET /api/locate/targets` lists them with their last solution
(`latest_emitter_solutions`).

### 4.2 Automatic tuning

Rule, unless overridden: sample rate 2 MS/s; centre = target − 100 kHz rounded to 1 kHz. This
is the same offset as `uhf_469_narrow`: it keeps the target off the DC spike and well inside
the inner 80 % that is measured. The chosen values are stored with the target (4.4) and shown
under Advanced.

### 4.3 Generated band profile

A profile for the window target ± 250 kHz with the detection, segmentation and comparison
values of `uhf_469_narrow.yaml` (themselves those of `central_800_narrow`). It is written once
to `<output>/bands/target_<key>.yaml` and passed to `resolve_band_profile` by path, so every
run names a profile that still exists and can be re-read. It is not written to `config/bands/`.

### 4.4 Campaign per target, and the gain that belongs to it

Today the write campaign is fixed for the life of the process (`FieldSettings.campaign_id`),
on purpose. The new flow switches it when the operator picks a target, so this is the most
safety-relevant change here. Rules:

- The switch happens only when no job is running, under the same device-transition lock
  that a capture start takes. It can never land between two bins.
- Campaign id `loc-<key>[-n]`. "Start over" increments `n`.
- **Additive table `locate_targets`** (`emitter_key`, `campaign_id`, `center_frequency_hz`,
  `sample_rate_hz`, `band_profile_path`, `if_gain_reduction_db`, `lna_state`,
  `calibrated_at_utc`, `calibration_path`). A restart resumes a target at its own gain, so a
  campaign stays at one gain across days without copying lines into a site profile.
- **Settled in PR 1: a deployment bound to a project (`FIELD_PROJECT`) refuses Locate.** Each
  campaign there needs a manifest under the project directory, which the installer makes
  root-owned (`/etc/dmr-field`), and the service does not write manifests. The refusal names
  `fieldctl campaign new` / `fieldctl campaign use`. An unbound deployment works.
- A restart comes up on its startup campaign (`FIELD_CAMPAIGN`), not on the last target.
  Choosing the frequency again resumes that target's campaign, tuning and gain.
- `FIELD_CAMPAIGN`, `/classic` and the CLI keep their current meaning. A service started with
  `FIELD_CAMPAIGN` opens on that campaign until a target is picked.

### 4.5 Calibration

`start_gain_calibration` already accepts `target_frequencies_hz`; the new flow passes
`[target]`. The P25 placeholder import is no longer needed. The result is written to
`locate_targets`, and calibration still refuses a campaign that already has measurements.

### 4.6 The drive

`start_live` with the target's tuning, profile and gain, and with
`spread_windows_over_bin = true`, the setting Stage 3.3 recommends for field drives. The
default for `/classic` and the CLI is unchanged. The HUD's "heard / not heard, dB" comes from
matching the closed bin's observations to the target within its tolerance. The pull-over hint
uses the same `near_threshold` list as today, matched to the target instead of the registry.

### 4.7 Solve and plan, on request

`POST /api/locate/solve` → a job running `solve_emitters(emitter_keys=[key],
scope=campaign, output_root=…)` with the emitter defaults (excess SNR, L = 500 m). It returns
the regions as GeoJSON.

The plan needs a small additive change in `geo/emitters.py`. Today `solve_emitters` discards
the posterior surface (`row, _ = _solve_emitter(...)`). It gains `plan=False`; when true, the
surface is passed to `build_target` / `plan_next_stops`, exactly as `geo/pipeline._plan` does
for sites, including its refusal to plan from an under-determined fit. Default off, so
`geo emitter solve` output is unchanged. A test pins that.

## 5. Front end

- New page at `/`: Hebrew, `dir="rtl"`, CSS logical properties. Leaflet and the tile proxy are
  reused unchanged. The current page is served at `/classic` byte-for-byte.
- One layout: in portrait, the map is on top with a bottom sheet; in landscape or on a tablet,
  the panel is on the right and the map on the left. Buttons are at least 56 px, and drive
  controls are reachable with one thumb.
- Voice uses `speechSynthesis` with a `he-IL` voice when the device has one. Without one,
  short tones mark events, and the screen says Hebrew voice is unavailable on this device.
- Screen kept awake with the Wake Lock API while driving. It needs HTTPS, which drive mode
  already requires.

## 6. Delivery plan

Each PR keeps every existing test passing unmodified, and `ruff` stays clean.

1. **Server: targets, tuning, campaign switch, stored gain** (4.1–4.5), with tests for the
   switch rules and for a restart resuming a target at its gain.
2. **Drive and result: the target's drive defaults and HUD, the solve endpoint, and a plan
   for emitters** (4.6–4.7), with tests that `plan=False` is unchanged and that an
   under-determined fit is not planned from.
3. **Front end: the new page** (section 3), with `/classic` unchanged.
4. **Field procedure**: `FIELD-DRIVE-USB-GPS-AND-GAIN.md` rewritten around the new screen; the
   command-line route kept as the fallback.

The first drive after PR 3 should be a validation drive against a transmitter with a known
position (3.5), as Phase 8 §4.3 asks.

## 7. Implemented in PR 1

`geo/locate.py` holds the rules and the `locate_targets` table; `web/service.py` composes it
(`select_locate_target`, `locate_targets`, and two hooks in the gain calibration);
`web/server.py` adds `GET /api/locate/targets` and `POST /api/locate/target`. Tests:
`tests/test_geo_locate.py`, `tests/test_web_locate.py`.

Choices made while building it:

- **An emitter already declared at the frequency is reused**, under its own key (`cc469`
  declared from the CLI becomes campaign `loc-cc469`). A declared emitter *within tolerance*
  but not at the frequency is refused by name. Declaring a second one beside it would make
  every observation between them `ambiguous`, and neither could ever be solved.
- **The gain is stored when the campaign opens**, as the gain in force then
  (`gain_source = in_force_at_creation`), and replaced by a calibration while the campaign
  has no runs. A campaign driven without calibrating therefore still resumes at the gain it
  was measured with.
- **The tuning of a campaign that holds runs cannot change**; "start over" opens the next
  campaign for that.
- **The generated band profile is written once** to `<output>/bands/target_<key>.yaml` and
  never rewritten, because runs name it. Its thresholds are pinned equal to
  `uhf_469_narrow.yaml` by a test.
- Choosing a target clears the in-memory drive drawing (bins, last solve). The database
  keeps everything.

`POST /api/locate/target` takes `frequency_mhz` and optionally `label`, `emission`,
`tolerance_hz`, `truth` (`{latitude, longitude, source}`, or `{}` to clear),
`center_frequency_hz`, `sample_rate_hz` and `start_over`. It answers 400 with the reason
when the target cannot be prepared, and 409 while a job runs or from a read-only view.

Not in PR 1: the drive defaults and per-target HUD of 4.6, and the solve and plan of 4.7.

## 8. Open points

- **Hebrew TTS on the phone in use.** Android's Google TTS has Hebrew; it is checked on the
  actual device before PR 3.
- **Several targets in one tuning** (later). The table and endpoints take one key per call,
  and the HUD has room for a small list. Nothing in PRs 1–3 assumes one target per process
  beyond the active one.
- **Intermittent targets** are supported through Advanced only. The HUD's "not heard" for them
  says "לא שודר / לא נשמע", since absence is not evidence for them.
