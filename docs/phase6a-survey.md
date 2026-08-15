# Phase 6A — generic RF survey

Given one wideband IQ recording, Phase 6A produces an objective, protocol-agnostic inventory of active RF signals with site and capture-time context, stored in SQLite, and can compare two runs — all without a P25 or DMR decoder involved or installed.

## Commands

```bash
dmr-surveyor survey run RECORDING --band central_800 --site home
dmr-surveyor survey list [--site home]
dmr-surveyor survey show RUN_ID
dmr-surveyor survey compare RUN_A RUN_B [--band central_800]
```

`survey run` requires `--band` and `--site`, resolved either as a path or as a name looked up under `config/bands/<name>.yaml` / `config/sites/<name>.yaml` relative to the current working directory. Ship your own site profile alongside `config/sites/home.example.yaml` (copy it, fill in real values — it is a template and is not meant to be used directly).

`--database` defaults to the same shared path used by the rest of the project's persistent inventory: `runs/inventory/dmr_inventory.sqlite3`. Point every `survey run`/`survey compare` invocation for a given deployment at the same database file so history accumulates.

`--hash-source` computes a SHA-256 of the source recording for provenance; it is off by default because hashing a multi-gigabyte capture is slow, and `survey_runs.source_path` plus the recording's own metadata are usually sufficient.

## Band and site profiles

A band profile (`config/bands/*.yaml`) describes *where to look* — never an expected frequency list or protocol:

```yaml
name: central_800
label: "800 MHz public safety (866-870 MHz)"
start_frequency_hz: 866000000
stop_frequency_hz: 870000000
raster_spacings_hz: [12500, 6250]
detection:            # overrides on top of DetectionSettings defaults
  scan_step_hz: 6250
  min_p95_channel_snr_db: 9.0
  # ...
segment_seconds: 2.0          # analyze this many seconds of IQ per segment
segment_stride_seconds: 10.0  # ...every this many seconds of the capture
max_segments: 200              # hard cap, bounds runtime and memory
usable_passband_rolloff_db: 3.0
comparison:
  frequency_tolerance_hz: 6250
  snr_delta_db: 3.0
  occupancy_delta_pct: 10.0
  persistence_delta: 0.25
  analyzed_seconds_ratio_limit: 4.0
```

A site profile (`config/sites/*.yaml`) records fixed measurement context: `site_id`, `label`, optional `latitude`/`longitude` (both left `null` if you'd rather not record location), `antenna`, `receiver`, `gain_mode`, `gain`, `notes`. A site with no recorded gain is flagged in the survey log as not gain-comparable — cross-run SNR deltas at that site are still computed, but should be read with that caveat.

## Segmented analysis and its two derived metrics

A 30-minute 866–870 MHz capture at 5 MS/s is roughly 36 GB. Analyzing every FFT frame in one pass is both slow and pointless for a fixed-strength survey signal — what matters is whether a signal is *there* and *how often*, not a single unbounded average. Phase 6A analyzes `segment_seconds` of IQ every `segment_stride_seconds`, up to `max_segments`, and detects candidates independently per segment using the same Phase 3 engine (`detect.features.feature_at`), then clusters detections across segments the same way Phase 3 already clusters across recordings (`detect.merge.candidate_clusters`).

This segmentation is what makes two metrics meaningful, and they are computed differently on purpose:

- **`occupancy_pct`** — the fraction of *analyzed FFT frames* (across the whole capture, weighted by each segment's FFT count) whose energy in the channel exceeded `noise_floor + occupancy_threshold_db`. Answers "how busy was it while we watched".
- **`persistence`** — the fraction of *survey segments* in which the candidate was *independently detected* as a local-maximum candidate. Answers "how often did it exist at all across the capture".

A short high-power burst gives low occupancy and low persistence. A control channel gives high occupancy and persistence near 1.0. An intermittent traffic channel that transmits briefly but reliably every stride interval gives low occupancy and high persistence. Every observation stores the inputs behind both numbers (`occupancy_threshold_db`, `occupancy_sample_count`, `segments_detected`, `segments_analyzed`) so a comparison between two runs can tell whether the numbers are actually comparable.

## Usable passband, not assumed Nyquist width

The Nyquist width of a recording (`sample_rate_hz`) is not the same as the usable RF bandwidth: the receiver's analog front end and any SDR-side decimation filter roll off before the edges. Phase 6A measures this per run rather than assuming full Nyquist width is analyzable — it builds a weighted-mean aggregate PSD across segments, smooths it with the same windowed-median used for the noise floor (amplitude-invariant to narrow signal peaks/nulls), and walks outward from the center to find where the response drops `usable_passband_rolloff_db` below the passband median and does not recover. This is a heuristic approximation, not a calibrated measurement, and is reported alongside its inputs (`reference_level_db`, `rolloff_db`) for transparency.

Every run records `usable_low_hz`, `usable_high_hz`, and `coverage_status` (`complete` | `partial` | `unknown`). A partial run lists the uncovered sub-ranges rather than silently analyzing less of the requested band than asked for. `survey compare` refuses to treat a frequency outside either run's measured usable passband as comparable — it is reported as `NOT_COMPARABLE`, never `MISSING_THIS_RUN`, because "outside our measured passband" is a different claim from "we looked and it wasn't there".

## Power units

Every power value derived from the PSD is named with its unit and carries `power_unit`/`calibrated` columns: `peak_dbfs_per_hz`, `average_dbfs_per_hz`, `noise_floor_dbfs_per_hz`, with `power_unit="dbfs_per_hz"` and `calibrated=false`. No column is ever named `*_dbfs` while holding a spectral density, and dBm is never emitted — that would require a calibration record this project does not yet have.

## Comparing runs

```bash
dmr-surveyor survey compare RUN_A RUN_B --band central_800
```

Works with no protocol decoder installed. Joins the two runs on the shared `rf_frequencies` catalog (already tolerance-matched at import time) and, once run-level comparability preconditions pass (same site, overlapping requested band, same `occupancy_threshold_db`, same detection settings, `analyzed_seconds` within `analyzed_seconds_ratio_limit`), emits one of:

| Status | Meaning |
|---|---|
| `NEW` | in target, not in baseline |
| `MISSING_THIS_RUN` | in baseline, not in target *this capture* |
| `STABLE` | in both, within tolerance |
| `SNR_CHANGE` | \|Δ SNR\| exceeds `snr_delta_db` |
| `OCCUPANCY_CHANGE` | \|Δ occupancy\| exceeds `occupancy_delta_pct` |
| `PERSISTENCE_CHANGE` | \|Δ persistence\| exceeds `persistence_delta` |
| `NOT_COMPARABLE` | a run-level or per-frequency precondition failed; `reason` says which |

`MISSING_THIS_RUN` is deliberately phrased per-run: a 30-minute capture not containing a signal that was present in an earlier run does not mean the signal no longer exists.

## Database schema

Added to the shared database (`inventory.store.connect_database()` runs first; nothing pre-existing is altered):

```
sites               site_id, label, coordinates (optional), antenna, receiver, gain
survey_runs          one row per survey: site, band profile, source, capture time + provenance,
                      requested vs. measured-usable band, coverage_status, analyzed_seconds,
                      detection settings, tool version
rf_frequencies        catalog only: nominal_frequency_hz, first/last_seen_at + run id, counts.
                      No protocol, system, site, or role column, ever -- the same RF frequency
                      can belong to different systems/sites/protocols at different times.
rf_observations       one row per (survey_run, rf_frequency): every measured field, keyed
                      UNIQUE(survey_run_id, rf_frequency_id)
run_comparisons        stored `survey compare` output
```

Rules enforced in code, not just convention:

- **Idempotency.** Re-importing a `survey_run_id` deletes and re-inserts that run's `rf_observations`; `rf_frequencies` first/last-seen columns are *recomputed from surviving observations*, including for frequencies the previous version of the run touched but the new version does not — never incremented in place, so a deleted run's timestamps cannot linger.
- **Time is capture time, never run ID.** `capture_start_utc` comes from the SDRplay `auxi` chunk when present, else a parsed `YYYYMMDD_HHMMSS` filename pattern, else is recorded `unknown` and excluded from first/last-seen computation (counted separately in `undated_observation_count`). Importing an older capture after a newer one still produces correct history.
- **Frequency identity stays protocol-neutral.** `rf_frequencies` matches an existing catalog row within a tolerance (not exact float equality, since the same physical channel measures slightly differently each run) rather than creating a new row per run.

## Output layout

```
runs/<output>/
├── run.json                 canonical manifest (peak RSS, elapsed, coverage)
├── reports/
│   ├── report.json          machine-readable
│   ├── report.md            human-readable
│   └── comparison_<A>_<B>.{json,md}   when `survey compare` is run
└── logs/survey.log          explicit stage transitions, never a bare "FAILED"
```

## Capture baseline for 866–870 MHz

Planned baseline for the next controlled 800 MHz capture:

```
center frequency: 868.000 MHz
sample rate:      5.000 MS/s
AGC:              OFF
manual gain:      TBD after the first controlled capture
```

At 868.000 MHz / 5 MS/s the band edges sit at ±2.000 MHz against a 2.500 MHz Nyquist half-width — 500 kHz of margin per side before the default 150 kHz edge exclusion. This has not been confirmed sufficient: the usable-passband measurement above must be run against a real 5 MS/s capture before the rate is finalized, since Nyquist width is not the same as usable RF width. If the measured usable width cannot reliably cover 866–870 MHz, the recommendation moves to a higher rate or two overlapping captures.

## Validating against a real recording

The unit test suite is exclusively synthetic (`tests/fixtures/synthetic.py` generates small deterministic fixtures at test time; no IQ data is committed). Real 800 MHz validation is optional and separate:

```bash
DMR_SURVEYOR_TEST_RECORDING=/path/to/p25_866_870_20260808_214241_867881250HZ.wav \
    pytest tests/test_survey_real_recording.py -v
```

This test is skipped — not failed — when the environment variable is unset or the file does not exist. The recording itself must never be added to the repository.

## Acceptance criteria

1. All pre-Phase-6 tests pass unmodified.
2. An existing wideband IQ file can be surveyed.
3. Active RF signals are produced without a prior frequency list.
4. A new run is stored in SQLite with site and capture-time context.
5. A second capture can be imported and accumulates.
6. `survey compare RUN_A RUN_B` works with no P25 decoder installed.
7. The existing DMR workflow is unbroken.
8. No P25 decoder is required to complete Phase 6A.
9. `run_spectrum()` with default settings produces the same artifact set as before (structurally guaranteed — see `docs/phase6-design.md`).
10. Peak RSS during a survey is recorded in `run.json`.
11. No dBm anywhere; every power value is `*_dbfs_per_hz` with `power_unit`/`calibrated`.
12. Importing an older capture after a newer one yields correct `first_seen_at`/`last_seen_at`.
13. `rf_frequencies` carries no protocol, system, site, or role column.
14. Every run records `usable_low_hz`, `usable_high_hz`, and `coverage_status`.
