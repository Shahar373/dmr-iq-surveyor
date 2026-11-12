# Phase 8 — fixed-frequency emitter geolocation (design)

Status: **Stages 1–3 implemented; Stage 4 items 1–2 done; the rest planned.**

This document is authoritative for Phase 8. It re-scopes the geolocation work around one question:
*where is the transmitter that is on the air on this frequency?* It does not replace Phase 7 — the
P25 registry, `geo solve`, the field app and every stored table keep working exactly as before.

## 1. Goal and scope

Locate passive, receive-only, fixed-frequency transmitters from levels measured at several places,
and make the accuracy of that answer **measurable**, so that every later improvement can be judged
by a number rather than by how the map looks.

In scope:

- a protocol-neutral target: "the emitter on frequency *f*" (Stage 1);
- a calibration harness that measures whether the reported credible regions are honest (Stage 2);
- measurement-bias fixes, a better statistical model, bearings and a terrain model (Stages 3–6).

Not in scope, deliberately:

- **No protocol decoding.** The user's emitters are identified by frequency. Phase 6B (P25 decoding)
  is not a prerequisite for anything here and is not on this roadmap.
- **No transmission of any kind.** A calibration emitter is a third-party transmitter whose position
  is published or otherwise known — never one we key up.
- **No UI ahead of need.** Stages 1–2 add CLI commands only; the field app is unchanged.
- **No learned models.** The estimator stays the explainable Bayesian grid of Phase 7.

## 2. Why a new target type instead of the P25 registry

Phase 7 attaches every measurement to a `p25_sites` row, which requires a WACN, a System ID, an RFSS
and a Site number. A fixed-frequency carrier has none of those. Filling them with invented values
would work mechanically but would put false facts into a table whose semantics `CLAUDE.md` forbids
changing. Phase 8 therefore adds its own tables, alongside the P25 ones, and reuses the estimator,
the contouring, the run exclusions, the gain/noise-floor drift checks and the common-mode correction
unchanged.

"Discovery before reference" still holds: `survey run` measures the band with no target list in
hand. Emitter targets are matched to stored observations only afterwards, when measurements are
materialised.

## 3. Stage 1 — emitter targets (implemented)

### 3.1 Schema (additive)

Applied by `geo.emitters.connect_emitter_database()`, which first applies the full Phase 7 schema,
so an existing database upgrades in place.

```
emitter_targets        one row per declared emitter: key, label, frequency, tolerance,
                       emission (continuous | intermittent), optional known position (truth)
emitter_measurements   one row per (survey_run, emitter): level or censoring level, position,
                       usability, quality flags -- same columns and meanings as geo_measurements
emitter_solutions      one row per (solve batch, emitter): the same fields as geo_solutions,
                       plus truth_json (the error against a known position, when one is on record)
```

No existing table is altered.

### 3.2 Attribution

| attribution | meaning |
|---|---|
| `frequency_match` | the observation lies within the emitter's tolerance, and no other declared emitter's tolerance window overlaps it |
| `ambiguous_neighbour` | another declared emitter is close enough that one observation could belong to either — excluded with usability `ambiguous` |

Attribution by frequency is *the* identification for this workflow, not a fallback. It still
assumes one transmitter per frequency in the analysed area; every solution records
`source_model: single_transmitter_assumed`, exactly as Phase 7 does, so the assumption stays visible.

### 3.3 Usability

The Phase 7 rules apply unchanged (`not_covered`, `no_position`, `run_excluded`,
`level_unreliable`, `receiver_artifact`). One rule is new:

- **`absence_not_evidence`** — an emitter declared `intermittent` that was *not* detected at a stop.
  For a continuous carrier, "looked and heard nothing" bounds the level from above and is real
  evidence. For an intermittent one it may only mean "it was not transmitting just then", so the
  row is kept, with the reason, and the solver does not use it. Detections of an intermittent
  emitter are used normally. (Stage 3 revisits this with occupancy-aware dwell.)

### 3.4 Known positions ("truth")

An emitter may carry `truth_latitude`/`truth_longitude` and a `truth_source` describing where the
position came from (a licence database, a published site list, a surveyed mast). Truth is **never**
read by the solver; it is compared with the result afterwards:

- `mode_error_m`, `mean_error_m` — distance from the posterior mode and mean to the known position;
- `inside_50`, `inside_90` — whether the known position lies inside the 50 % / 90 % credible region.

This is how field accuracy is measured without trusting the method being measured.

### 3.5 Commands

```
dmr-surveyor geo emitter add KEY --frequency-mhz F [--label] [--tolerance-hz]
                                 [--emission continuous|intermittent]
                                 [--truth-lat --truth-lon --truth-source] [--notes]
dmr-surveyor geo emitter list
dmr-surveyor geo emitter remove KEY
dmr-surveyor geo emitter measurements [--run-id ...] [--campaign] [--level-metric]
dmr-surveyor geo emitter solve [--emitter KEY ...] [--campaign] [--output-root]
```

`geo emitter solve` rebuilds the emitter measurements of its scope first (pure SQL, cheap), so a run
excluded or re-imported since the last solve can never be read stale.

## 4. Stage 2 — accuracy measurement (implemented)

### 4.1 What "accurate" means here

The product is a credible region, so there are two separate questions, and both must be answered:

1. **Is the region honest?** A 90 % region should contain the transmitter in about 90 % of
   independent trials. Below that, the method is *overconfident* — the worst failure for this tool,
   because a small wrong polygon sends someone to the wrong place.
2. **Is it useful?** Given honesty: how large is the region, and how far is the mode from the truth?

Shrinking a region without keeping (1) is not an improvement.

### 4.2 Simulation harness — `geo calibrate`

`geo.calibration.run_calibration()` draws independent trials from a declared scenario, solves each
one with the production solver (`solve_site`, unchanged), and reports:

- status counts (`ok`, `unbounded_region`, `weak_geometry`, `insufficient_evidence`);
- empirical coverage at 50 % and 90 %, with a binomial standard error and a verdict:
  `consistent`, `overconfident` or `conservative` (more than two standard errors from nominal),
  or `too_few_trials`;
- median and 90th-percentile mode error, median 90 % area.

Scenario knobs: stop count and layout (`ring`, `random`, `one_sided`, `line`, `drive_loop`), radius,
transmitter offset, true reference level, true path-loss exponent, true shadow-fading sigma, the
detector's censoring level, and **spatially correlated shadowing** (`--correlation-m`, exponential
Gudmundson model). The last one matters: Phase 7's likelihood treats stops as independent, and
drive bins 150 m apart are not. The harness can now show how much that costs in coverage, which is
the evidence Stage 4 needs.

Runs are deterministic for a given seed. Each trial gets its own child seed, so adding trials never
changes earlier ones.

### 4.3 Field truth

`geo emitter solve` writes `truth_json` for every emitter with a known position and prints the
error and containment. A campaign against two or three public transmitters of known position is the
recommended first field validation, before any algorithm change.

### 4.4 First findings (simulation)

Measured with the harness on the test-grade grid (8 km margin, 800 m coarse / 200 m fine cells),
20–30 trials per row, true sigma 6 dB unless stated. These are simulations from the model's own
family; they bound what the method can do, not what it will do in the field.

| scenario | 90 % coverage | verdict | median mode error | median 90 % area |
|---|---:|---|---:|---:|
| ring, 12 stops, all heard, independent | 95 % | consistent | 0.8 km | 178 km² |
| random, 16 stops, some unheard, sigma 4 dB, independent | 100 % | consistent | 0.6 km | 199 km² |
| random, 16 stops, some unheard, correlated 1.5 km | 97 % | consistent | 0.9 km | 199 km² |
| drive arc, 30 bins @ 300 m, all heard, independent | 90 % | consistent | 4.4 km | 129 km² |
| drive arc, 30 bins @ 300 m, all heard, correlated 600 m | **50 %** | **overconfident** | 4.2 km | 93 km² |
| drive arc, 40 bins @ 150 m, some unheard, independent | **77 %** | **overconfident** | 4.1 km | 144 km² |
| drive arc, 40 bins @ 150 m, some unheard, correlated 500 m | **30 %** | **overconfident** | 3.8 km | 106 km² |

What this says, in order of consequence:

1. **Stationary stops spread around the target are honest.** Regions are large (the 90 % region
   usually reaches the analysed edge, so the solver says `unbounded_region` rather than `ok`), but
   they contain the transmitter as often as they claim to.
2. **Drive data is overconfident.** Dense bins along one arc of road, with shadowing correlated
   over a few hundred metres as it is in reality, give 90 % regions that contain the transmitter
   30–50 % of the time. Even with independent shadowing, 40 closely spaced bins along one arc fall
   below nominal. This is the most important accuracy problem found so far, and it is a modelling
   problem, not a measurement one — Stage 4's correlated-shadowing work is now motivated by a
   number rather than by an argument.
3. **Geometry beats count.** 12 stops around the target do better than 40 bins along one side.
   `geo plan`'s azimuth-driven stop selection matters more than adding drive time.

Stage 4.1 (below) addresses this; `geo emitter solve` now models the correlation by default.

## 5. Later stages (planned)

Each stage is a separate PR, and each must show its effect with `geo calibrate` (and field truth
where available) before it is merged.

### Stage 3 — measurement biases

From `docs/known-issues-v0.10.md`:

- ~~merged segments take `max` SNR instead of a weighted mean~~ — done, 3.1;
- ~~channel SNR is (S+N)/N rather than excess/N~~ — done, 3.1;
- ~~GPS accuracy is stored but unused, and not stored at all for drive bins~~ — done, 3.2;
- ~~the browser fix timestamp is not forwarded, so a stale repeated fix looks fresh~~ — done, 3.2;
- ~~the drive-bin window cap keeps the first N windows rather than a spread sample~~ — done
  (opt-in), 3.3.

**Each fix adds a new field or metric next to the existing one; existing labels and values keep
being emitted unchanged**, as `CLAUDE.md` requires.

#### 3.1 The level — implemented

A new level, `excess_snr_db`, is measured beside the Phase 7 fields and is the default for
`geo emitter` (`--level-metric`; P25 `geo measurements` / `geo solve` keep `snr_db`):

- per window, `10 log10( mean(max(S+N − N, 0)) / mean(N) )` over the channel — **S/N**, not (S+N)/N
  (`detect.features`: `average_excess_snr_db`, floored at −60 dB);
- across the windows that detected the channel, the **median**, not the maximum
  (`detect.merge`: `excess_snr_db`, `excess_snr_windows`);
- stored as `rf_observations.excess_snr_db` (added in place; NULL on older rows);
- non-detections are censored at the detector's `(S+N)/N` gate converted to the same scale,
  `10 log10(10^(T/10) − 1)` — 4 dB becomes 1.8 dB;
- a run stored before the column existed has its excess **derived** from `snr_db` (exact conversion,
  but still carrying that field's max-over-windows bias), and the measurement is flagged
  `level_derived_from_snr_db`.

How large the two biases are:

| bias | size |
|---|---|
| (S+N)/N instead of S/N | +3.0 dB at a true 0 dB, +1.2 dB at 5 dB, +0.4 dB at 10 dB — weak (far) stops read closer |
| max over N windows, per-window spread σ | +0.85σ at 3 windows, +1.16σ at 5, +1.54σ at 10 (median: 0) — e.g. +3 dB for a 10-window drive bin at σ = 2 dB |

The second varies with how many windows a stop or bin happened to get, so it is not even a constant
offset that `P0` could absorb. Both are removed by construction in `excess_snr_db`. Because the
detector only puts windows that *saw* the channel into the cluster, the median is also an
occupancy-gated level for an intermittent emitter: it is measured while the emitter is on.

Still open for intermittent emitters: a dwell long enough that a non-detection can be admitted as
evidence.

#### 3.2 Where a measurement was taken — implemented

**Fix age.** The page now sends `fix_age_s` = `Date.now() − fix.timestamp`, two readings of the
phone's *own* clock, so any offset that clock has cancels. The Pi backdates the fix by that age on
its monotonic clock (`web/service.py: push_live_position`). A location provider that repeats its
last fix — the most likely cause of the "same bin repeating" field symptom — now sees that fix age
and go stale after `live_max_position_age_seconds`, so its windows are dropped rather than placed
at a spot the car has left. The response says `fix_age_s` and `stale`. A phone that sends no age,
or a malformed one, is treated exactly as before; ages are clamped to [0 s, 1 day]. The HUD's
"last fix" age also counts from when the fix was taken.

**Accuracy.** Each drive window carries the worse reported accuracy of its two fixes; a bin stores
the median as `survey_runs.gps_accuracy_m` (NULL where the source reports none, e.g. a USB receiver
speaking RMC/GGA). Emitter measurements derive `position_sigma_m` =
√((accuracy/2.45)² + (spread/2)²): the 95 % GPS radius as a one-sigma, plus half the road a drive
bin's level was averaged over.

**Using it in the solver** — implemented, measured, and left **opt-in**
(`geo emitter solve --model-position-uncertainty`). Near a stop, position error σₚ adds
`(10n/ln10 · σₚ/d)²` to a measurement's level variance; the solver applies that per cell
(`_positional_terms`) only when some measurement carries σₚ > 0, so every other solve is untouched.
Measured (16 random stops with non-detections, 30 trials):

| position error | 90 % coverage, not told / told | median mode error, not told / told |
|---|---|---|
| 0 m | 100 / 100 % | 246 / 246 m |
| 100 m | 100 / 100 % | 299 / 269 m |
| 250 m | 100 / 100 % | 315 / 337 m |

Regions of order 100 km² and 4–8 dB shadowing dominate; positions wrong by up to 250 m did not make
them dishonest, and modelling it gave no consistent gain for ~35 % more solve time. It stays
available for campaigns with stops very close to a suspected site.

#### 3.3 How much road a drive bin covers — implemented (opt-in)

`LiveSettings.spread_windows_over_bin` (field app: `live_spread_windows_over_bin`) keeps a window
only once the receiver has moved `span / max_windows_per_bin` since the last kept one, and skips it
before the FFT otherwise — so in slow traffic the ten windows are spread over the whole 150 m
instead of the first ~40 m. A receiver that stays put for 2 × `max_windows_per_bin` windows is
treated as stopped and filled as before, so a parked measurement still lands. It is **off by
default** because existing tests pin the earlier behaviour (a parked receiver writes after exactly
`max_windows_per_bin` windows); turning it on for field drives is recommended.

Whatever the setting, every drive bin now records `under_spanned` (covered less than half its
nominal span, judged from `position_spread_m`), counted in `LiveStats.bins_under_spanned`, and
emitter measurements from such a bin carry the quality flag `under_spanned_bin`.

### Stage 4 — statistical model

Given the findings in §4.4, the first item below is now the highest-value accuracy change on this
roadmap and is recommended *before* Stage 3.

#### 4.1 Correlated shadowing — implemented

`SolveSettings.shadowing_correlation_m` weights every measurement by
`w_i = 1 / sum_j exp(-d_ij / L)` over all measurements (heard or not). A cluster of stops that share
their shadowing counts as about one stop; a stop kilometres from any other keeps weight ~1. It is a
weighted composite likelihood: the detection count becomes the weight sum wherever it appears in the
`P0` marginalisation and the sigma normaliser, and each censored term is scaled by its weight. It
widens the posterior to what the data supports; it does not model the shared shadowing itself.

- `geo emitter solve` assumes `L = 500 m` by default (`--shadowing-correlation-m`).
- `geo calibrate --model-correlation-m` sets the solver's `L`; `--correlation-m` is the simulated truth.
- Phase 7's `geo solve` is unchanged (`L = 0`); a test pins that `L = 0` is bit-identical to before.
- Each solution's diagnostics record `shadowing_correlation.effective_measurements`.
- Memory is bounded (review of 2026-11-12). The weights are summed in row blocks of at most 2^20
  pairs, visiting only stops within 40 L in east (each skipped pair is worth exp(-40)), instead of
  dense N × N × 2 and N × N arrays (~1 GB at 5,000 measurements). `_evaluate_grid` now sizes its
  cell chunk by the measurement count as well as the offset count; sized by offsets alone it built
  65,536 × N temporaries. Measured with production settings along a drive: peak 530 MB / 2.1 GB /
  4.3 GB at 250 / 1,000 / 2,000 measurements before, 162 MB at all three after. Results match the
  dense code to within 5e-13 relative, and `L = 0` solves are still bit-identical
  (`tests/test_geo_solver_memory.py`). Memory is bounded; time is not: a few thousand measurements
  with many non-detections still take minutes to hours.

Measured (test-grade grid, 30 trials per row, true sigma 6 dB):

| scenario | true L | 90 % coverage at model L = 0 / 300 / 600 m | median 90 % area at 0 / 300 / 600 m |
|---|---:|---|---|
| drive arc, 30 bins @ 300 m, all heard | 0 | 90 / 100 / 100 % | 135 / 211 / 297 km² |
| drive arc, 30 bins @ 300 m, all heard | 200 m | 80 / 97 / 100 % | 128 / 208 / 292 km² |
| drive arc, 30 bins @ 300 m, all heard | 600 m | **57** / **77** / 100 % | 93 / 170 / 265 km² |
| drive arc, 40 bins @ 150 m, some unheard | 0 | **77** / 100 / 100 % | 144 / 285 / 329 km² |
| drive arc, 40 bins @ 150 m, some unheard | 500 m | **30** / 97 / 100 % | 106 / 248 / 320 km² |
| ring, 12 stationary stops | 0 | 93 / 93 / 100 % | 177 / 180 / 207 km² |
| random, 16 stationary stops, some unheard | 0 | 100 / 100 / 100 % | 253 / 266 / 315 km² |

Reading: assuming at least the true decorrelation distance restores honest coverage on drive data,
at the price of roughly doubling the drive region — which is the size it should always have been.
Stationary stops spread kilometres apart are almost unaffected. 500 m was chosen as the default
because honesty comes first: 300 m already reached ~nominal 90 % coverage for a true 500 m but
under-covered at 50 % (33 %). In open or rural terrain, where shadowing decorrelates more slowly,
raise it. Mode error is unchanged by the weighting; it widens regions, it does not move them.

#### 4.2 Outlier stops — measured; a robust diagnostic, not a likelihood change

The question: does one badly wrong stop (behind a building, or with line of sight to a hilltop) drag
the region, and would a heavy-tailed likelihood fix it? The harness gained `outlier_fraction` /
`outlier_sigma_db` (`geo calibrate --outlier-fraction --outlier-sigma-db`) to simulate it. A
two-component mixture likelihood (inliers N(0, σ²), outliers 4σ wide) was implemented and measured:

| test | Gaussian (existing) | mixture likelihood |
|---|---|---|
| ring of 12, 15 % outlier stops: 90 % coverage | 97 % | 97–100 % |
| ring of 12, 15 % outlier stops: median mode error | 1.5 km | 1.2–1.4 km |
| 10 stops, one stop 25 dB off: median mode error (12 campaigns) | 1,142 m | 1,141 m |
| same: the bad stop identified | never by a plain residual threshold | 12 / 12 |
| solve time | 1× | ~35× |

Reading: the existing solver is *already honest* under outliers, because marginalising over σ lets
it widen σ (true 4 dB, fitted 8–10 dB with one wild stop) — a larger, still truthful region. The
mixture did not locate the transmitter better, and cost 35× the time, so it was **not adopted**.

What it did show is that the bad stop is identifiable, which the Gaussian fit hides. A free
equivalent was adopted instead: residuals at the best fit are compared with their median using a
spread from the median absolute deviation (floored at 2 dB), and any detection ≥ 3 robust SDs away
is marked `suspect` (`diagnostics.suspect_stops`, `residuals[].robust_z`, a warning, and a line in
`geo emitter solve`'s output). Measured on 40 campaigns of 10 stops: the 25 dB stop named in 35;
40 clean campaigns raised 4 flags in total. It needs at least 5 detections and never removes or
down-weights anything — it tells the operator where to re-measure.

- ~~spatially correlated shadowing in the likelihood~~ — done, see 4.1 above;
- ~~heavy-tailed residuals~~ — measured; adopted as a diagnostic, not a likelihood change. See 4.2 below;
- a stop-type offset (vehicle roof vs. handheld) marginalised like `P0`;
- leave-one-out common-mode correction.

### Stage 5 — bearings

A directional antenna rotated at three or more stops gives a bearing with an uncertainty. The
solver's likelihood is a product of terms; a von Mises bearing term is one more factor. Expected
effect: roughly an order of magnitude smaller regions (`docs/TRANSMITTER-LOCATION-STUDY.md`).

### Stage 6 — terrain model

Replace the log-distance mean `mu(cell, stop)` with a terrain-aware path loss computed from a DEM
(Copernicus GLO-30 or SRTM, downloaded ahead of time for offline use) and a diffraction model
(Deygout first; ITM/Longley-Rice if Deygout is not enough). `P0` stays marginalised; the
log-distance model stays as the fallback and as the comparison baseline. Profiles are computed per
(coarse cell, stop) and cached, and the heavy pass is expected to run on a laptop rather than the
Pi. This stage is worth doing only if Stage 2 shows a measurable gain on terrain-heavy scenarios
and field truth.

## 6. Acceptance for Stages 1–2

- all pre-existing tests pass unmodified, `ruff check .` clean;
- an emitter can be declared, measured and solved on a database written by an earlier version;
- `ambiguous_neighbour`, `absence_not_evidence` and every Phase 7 usability reason are recorded with
  a reason rather than dropped;
- truth is never an input to the solver (a test pins that changing it leaves the solution unchanged);
- `geo calibrate` is deterministic for a seed, and a test pins that independent shadowing in the
  model's own family is not reported overconfident while strongly correlated shadowing along a
  drive is.
