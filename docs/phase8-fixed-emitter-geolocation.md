# Phase 8 — fixed-frequency emitter geolocation (design)

Status: **Stages 1 and 2 implemented; Stages 3–6 planned.**

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

Until Stage 4 lands, treat a region built mostly from drive bins as optimistic.

## 5. Later stages (planned)

Each stage is a separate PR, and each must show its effect with `geo calibrate` (and field truth
where available) before it is merged.

### Stage 3 — measurement biases

From `docs/known-issues-v0.10.md`, all still open:

- merged segments take `max` SNR instead of a weighted mean (bias toward "closer");
- channel SNR is (S+N)/N rather than excess/N (compresses weak levels near the gate);
- GPS accuracy is stored but unused, and not stored at all for drive bins;
- the browser fix timestamp is not forwarded, so a stale repeated fix looks fresh;
- the drive-bin window cap keeps the first N windows rather than a spread sample.

Plus a level metric designed for a continuous carrier: integrated in-channel power above the local
noise floor, median across windows. **Each fix adds a new field or metric next to the existing one;
existing labels and values keep being emitted unchanged**, as `CLAUDE.md` requires.

For intermittent emitters: occupancy-gated levels (measure only while on) and a dwell long enough
that a non-detection can be admitted as evidence.

### Stage 4 — statistical model

Given the findings in §4.4, the first item below is now the highest-value accuracy change on this
roadmap and is recommended *before* Stage 3.

- spatially correlated shadowing in the likelihood (or an effective-sample-size correction);
- heavy-tailed residuals (Student-t) so one reflected or blocked stop cannot drag the region;
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
