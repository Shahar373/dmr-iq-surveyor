# Phase 7 design — multi-session P25 site geolocation

Phase 7 turns the Phase 6A survey inventory into per-site transmitter *location estimates*: a
posterior probability surface and credible-region polygons for each P25 site, built from several
passive recording sessions made at different places, improving as sessions accumulate.

It adds a reference layer (`reference/`), a geolocation layer (`geo/`), a `geo` CLI sub-app, and a
field web app (`web/`). It changes no existing command, table, label or test.

## 1. What the input actually supports

The starting point is an externally-produced snapshot of one P25 system
(`WACN BEE00`, `SysID 37D`) listing 26 sites. Three properties of that snapshot drive the whole
design:

| Property of the snapshot | Consequence |
|---|---|
| 20 of 26 sites have a primary control-channel frequency; 6 do not | Those 6 cannot be measured at all until their control channel is discovered. They are stored, and reported as `frequency_unknown` — not silently dropped. |
| `867.912500 MHz` is listed for **both** RFSS 1 / Site 50 and RFSS 1 / Site 82 | A power measurement on that frequency is a *mixture*. It cannot be attributed to one site. |
| NAC `371` appears on five different sites (34, 35, 53, 58, 84) | NAC does not identify a site either. Only the RFSS Status Broadcast in the control channel's TSBK stream does. |

So: **a received-power measurement at a frequency is not, by itself, a measurement of a site.** The
system must carry that distinction in the data, not in a footnote.

## 2. The attribution ladder

Every geolocation measurement stores exactly how it was attributed to a site. This is the Phase 7
expression of the project's "evidence-first" and "missing is not null" principles.

| `attribution` | Meaning | Used by the solver? |
|---|---|---|
| `decoded` | RFSS/Site read from control-channel decoder evidence in this very run | yes (reserved for a future P25 decode milestone; nothing emits it today) |
| `inferred_unique` | The frequency was measured, and exactly one site in the registry uses it | yes, flagged |
| `ambiguous_reuse` | The frequency was measured, but more than one registry site uses it | no — excluded with a reason |
| `frequency_unknown` | The site is known, but no control-channel frequency is on record | no — nothing to measure |

`decoded` exists in the schema from day one on purpose. Adding a real P25 control-channel decoder
later must not require a schema migration or a re-interpretation of stored rows — it only has to
start emitting a rung that already exists.

Independently of attribution, each measurement records its **usability**:

| `usability` | Meaning |
|---|---|
| `usable` | Inside this run's *measured* usable passband, and the run has a position |
| `not_covered` | Outside the run's measured usable passband. **This is not evidence.** We did not look there. |
| `level_unreliable` | Detected, but outside the measured passband, so the roll-off understates its level by an amount nothing here can bound. The solver reads level as distance, so an understated level would place the site further away *with confidence*. |
| `receiver_artifact` | Landed on the receiver's own DC/LO spike. Its level is a property of the radio, present at every stop, so matching it to a site would inject the same confident wrong measurement into the whole campaign. |
| `superseded_channel` | The site has more than one control channel on record. Two channels of one site measured from one place are not independent evidence, and the solver multiplies likelihood terms — so only one may count per stop. |
| `run_excluded` | The whole survey run was barred from geolocation (see below) |
| `no_position` | The run has no coordinates, so the measurement cannot be placed |
| `ambiguous` / `frequency_unknown` | Excluded by the attribution ladder above |

`edge_warning` is deliberately *not* an exclusion. It marks a fixed 150 kHz margin from the
recording's Nyquist edges (`SpectrumSettings.edge_exclusion_hz`) — an absolute width, not a measured
one, which at low sample rates covers the entire band. The measured usable passband is the honest
edge test and runs separately.

### Barring a whole run

`geo_run_exclusions` bars one survey run from contributing, with a reason. It is applied
automatically when a capture delivered materially less than it asked for, or suffered driver
overflows. The reasoning matters: a signal that was present but was not recorded long enough to be
detected would arrive as a **non-detection**, and a non-detection is evidence that pushes the site
*away* from that stop. A short capture would therefore not merely lose a measurement — it would
manufacture a confident wrong one. The run stays in the database as evidence; it stops counting.

### Gain drift

Levels recorded at different receiver gain are not on one scale, and the method is a comparison of
levels between places. The gain actually applied is stored per stop (not the site profile's
placeholder), and measurements from a stop whose gain differs from the campaign's modal value are
flagged `gain_differs_from_campaign`.

The `not_covered` versus `not_observed` split is the same honesty rule Phase 6A already enforces
between `NOT_COMPARABLE` and `MISSING_THIS_RUN`, applied to geolocation: *"outside our measured
passband"* is a different claim from *"we looked and heard nothing"*.

## 3. Detections and non-detections are both evidence

A run in which a site's control channel was **not** detected, on a frequency that *was* inside the
measured passband, is a genuine and often strongly constraining observation: the site is unlikely to
be near that point. Phase 7 models it as a **left-censored** measurement rather than discarding it.

- **Detection**: the level is the observation's channel SNR above the local noise floor
  (`snr_db`, relative `dBFS/Hz`-derived, never dBm — the project has no calibration record).
- **Non-detection**: all we know is `level < threshold`, where the threshold is the run's own
  detection setting (`min_average_channel_snr_db`) in the same units.

## 4. The estimator: Bayesian grid, not a point fit

For each site the unknowns are its position `p`, a reference level `P0` (an ERP/antenna proxy) and a
path-loss exponent `n`. The forward model is the log-distance model already documented in
`docs/TRANSMITTER-LOCATION-STUDY.md`:

```
mu(p, P0, n; x) = P0 - 10 n log10( max(d(x, p), d_min) / d0 )
```

with `d0 = 1000 m` and shadow fading modelled as zero-mean Gaussian with standard deviation `sigma`
(default 8 dB, a normal suburban 800 MHz value — configurable, never claimed as measured).

Per measurement `i` at position `x_i`:

```
detected      log L_i = log N( y_i ; mu_i , sigma )
not detected  log L_i = log Phi( ( y_thr,i - mu_i ) / sigma )
```

The posterior over position is obtained by evaluating this on a geographic grid and marginalizing
`P0` and `n` out with uniform priors:

```
P(p | data)  ∝  sum over n  sum over P0   exp( sum_i log L_i(p, P0, n) )
```

`P0` is *not* integrated on one global grid. The reference level implied by a detection varies by
over a hundred dB across a metro-scale region, so a global grid would have to be both very wide and
finer than `sigma` — unaffordable. Instead each cell integrates over offsets around **its own**
best-fit `P0`, using the exact reduction

```
sum_i (a_i - P0)^2  =  RSS(cell)  +  K * delta^2
```

where `a_i` is the reference level that would fit detection `i` exactly at that cell, `P0_hat` is
their mean, `delta = P0 - P0_hat` and `K` is the detection count. Only `RSS(cell)` varies per cell,
so the offsets are a small fixed vector, and the quadrature is fine relative to `sigma` rather than
merely wide. Where there are no censored terms the offset integral is a constant and drops out
entirely.

### Why a grid posterior and not least squares

- **It is the thing that "improves with every session".** Adding a session multiplies in new
  likelihood terms; the credible region shrinks by construction. No refit heuristics, no state to
  reconcile.
- **It keeps multimodality honest.** RSSI localization is genuinely multimodal (a hilltop site with
  line of sight looks like a near site). A point fit picks one lobe and reports a confident, wrong
  answer. A posterior keeps both lobes visible.
- **It handles censored non-detections natively**, which a least-squares residual cannot.
- **Its output is already the requested product**: iso-probability contours of the posterior *are*
  the polygons to draw on the map. A region clipped by the edge of the analysed area is closed
  along that edge, by contouring a surface padded with a below-threshold border — closing an open
  contour end to end instead draws a chord across the region and understates it (measured: 11.7 km²
  drawn for a region whose cells cover 29.3 km²), which is exactly the wrong direction for a tool
  whose job is to report uncertainty honestly.
- **It is explainable.** Every input is a stored number with a unit; no learned classifier is
  involved, per `CLAUDE.md`.

### Bounded runtime, coarse-to-fine

A metro-scale region at 100 m resolution is ~360k cells; times a `P0` grid, an `n` grid and every
measurement, a single-pass evaluation is far too slow for a Raspberry Pi. Phase 7 evaluates the
posterior in two bounded stages:

1. **Coarse pass** over the whole region at `coarse_resolution_m` (default 500 m).
2. **Fine pass** at `resolution_m` (default 100 m), restricted to the bounding box of the coarse
   cells holding `refine_mass` (default 0.999) of the posterior. The resolution adapts to the
   region rather than the extent being cropped: it coarsens until the grid fits `max_fine_cells`,
   and refines below `resolution_m` (down to `min_resolution_m`) when the region is small enough
   to reach `target_fine_cells`. A region sixty kilometres across gains nothing from 100 m cells;
   one two kilometres across deserves finer ones.

Both stages are deterministic, and the cell counts actually used are recorded in the solution's
diagnostics — bounded work with the bound written down, matching how `survey run` bounds segmented
analysis.

## 5. When the solver must refuse

A pretty polygon drawn from three measurements in a straight line is worse than no polygon. Every
solution carries a `status`:

| `status` | Raised when |
|---|---|
| `ok` | Enough evidence and geometry to report a bounded region |
| `insufficient_evidence` | Fewer than `min_detections` (default 3) usable detections |
| `unbounded_region` | The 90% credible region touches the edge of the analysed area — judged on the coarse pass, which spans the whole requested region, since the fine pass is a zoom into where the mass already is and its edges would flag every well-constrained site |
| `weak_geometry` | The measurement points span less than `min_azimuth_span_deg` (default 90°) in azimuth around the posterior mode — a one-sided view trades position against `P0` and cannot separate them |

Additional warnings never suppress a result, they annotate it: `not_gain_comparable` (a contributing
run has no recorded gain, so its levels are not on the same scale), `ambiguous_frequency_excluded`,
`partial_coverage`, `few_non_detections`, `simulcast_not_modelled`.

**Simulcast.** P25 800 MHz systems commonly transmit one logical site from several towers
simultaneously. There is then no point source, and this estimator will land somewhere inside the
cell rather than on a tower. Phase 7 does not model it and does not attempt to detect it; every
solution records `source_model: single_transmitter_assumed` so the assumption is visible in the
output rather than implied.

## 6. Realistic accuracy

With an omnidirectional antenna and sequential single-receiver measurements, this is a
**search-area-reduction** method, exactly as `docs/TRANSMITTER-LOCATION-STUDY.md` states. Expect a
90% credible region of order hundreds of metres to several kilometres, better with more sites and
better azimuth spread, worse in dense urban multipath or for simulcast sites. A reported mode is not
a tower coordinate and the reports never present it as one.

Directional bearings (Method B in the study document) would shrink these regions by roughly an order
of magnitude. The solver's likelihood is written so a bearing term can be added as another factor
without restructuring anything.

## 7. Schema (additive only)

Applied by `geo.store.connect_geo_database()`, which calls
`survey.store.connect_survey_database()` first (which itself calls the DMR
`inventory.store.connect_database()`), so an existing Pi database upgrades in place with no data
loss and no user action.

```
reference_snapshots   provenance of one imported external snapshot
p25_systems           (wacn_hex, system_id_hex)
p25_sites             (system, rfss, site) + observation_status, nac_hex, notes
p25_site_channels     site -> control-channel frequency, with role and evidence
geo_measurements      one row per (survey_run, p25_site, frequency): level or censoring level,
                      position, attribution, usability, quality flags
geo_run_exclusions    a survey run barred from geolocation, and why
geo_solutions         one row per (solve batch, site): status, mode, credible areas, diagnostics,
                      GeoJSON, and the exact run ids that produced it
```

Rules enforced in code:

- `rf_frequencies` is untouched and stays protocol-neutral. Nothing in Phase 7 adds a protocol,
  system, site or role column to it. The site↔frequency relation lives in `p25_site_channels`.
- **Discovery before reference.** `survey/` does not import `reference/` or `geo/`. The reference
  snapshot is read only when materializing measurements, long after observations are stored. Nothing
  in the registry can influence what the detector looks for.
- **Idempotency.** Re-importing a snapshot id replaces that snapshot's rows; re-materializing a run's
  measurements replaces that run's rows; re-solving with the same batch id replaces that batch.
  Same contract as `inventory.replace_run` and `import_survey_run`.
- Solution history is kept. Solve batches accumulate so the shrinking of a site's region across
  sessions is inspectable, not overwritten. "Latest" is by insertion order, never by timestamp: a
  Raspberry Pi has no real-time clock, so a solve run later in the day can carry an earlier
  timestamp than one run before it.

## 8. Field web app

A local, single-operator control surface served by the Pi and opened from a phone on the same
hotspot. It is deliberately built on the standard library's HTTP server: a field tool must not fail
because a dependency did not install, and the API surface is small.

```
GET  /                          the single-page app
GET  /api/state                 sites, latest solutions, recent sessions, device probe
POST /api/position              set the current operator position (browser GPS or a map tap)
POST /api/capture               start a capture -> survey -> measurements -> solve job
GET  /api/jobs/<id>             job status
GET  /api/jobs/<id>/events      server-sent progress events
POST /api/jobs/<id>/cancel      request cancellation
POST /api/analyse               run the same chain on a recording already on disk
POST /api/solve                 re-solve every site, optionally rebuilding measurements first
GET  /api/geojson               measurements + credible-region polygons for the map
GET  /api/sites                 registry with per-site evidence and solution status
```

The page shows the operator's position, a record button with live progress, the measurement points
coloured by level, and each site's credible-region polygons — with its `status` and warnings shown
next to it, never a bare polygon.

Static assets are served without a token so the page can load and read the token out of its own URL;
every `/api/` route requires it. `POST /api/analyse` exists so the whole chain is exercisable with no
SDR attached, which matters because a field tool must be verifiable before anyone drives anywhere
with it.

Capture is a real SDR capture through the existing `capture/` module, unchanged. With no SDR
attached the API reports the device probe error instead of pretending.

**Storage.** A 5 MS/s, 90 s stop writes 1.68 GiB, so a campaign cannot keep its recordings on the
storage a Pi in the field has. It does not need to: the recording is only required until the survey
has extracted its observations. Free space is checked *before* every capture and a capture that does
not fit is refused with the numbers; after a survey succeeds, recordings beyond `keep_recordings`
(default 1) are deleted and each deletion is written to a ledger, while every capture's
`*_capture_report.json` stays behind. A failed stop keeps its IQ.

**Preconditions are checked before the operator pays for a capture.** Band and site profiles are
resolved, disk is checked, and the device is probed at submit time — not inside the job, after the
90 seconds have already been spent.

## 9. Scope

Passive and receive-only, unchanged. Phase 7 adds no transmit, no injection, no decryption. It reads
recordings and a frequency list and produces probability surfaces. Reports state uncertainty and
alternative explanations, and never present a posterior mode as a confirmed transmitter coordinate.
