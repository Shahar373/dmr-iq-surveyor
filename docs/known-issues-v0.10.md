# Known issues — v0.10.0 (experimental geolocation)

This document lists issues found while auditing the geolocation branch (`stabilize/p25-geolocation-v0.10`,
based on 36 commits merged into `main` as v0.10.0) before any of them were fixed. **Geolocation
maturity: experimental.** None of these are fixed in v0.10.0; each is a candidate for a dedicated,
reviewed PR with its own characterization test, failing test, and compatibility analysis. Nothing
here should be read as a commitment to a specific fix or timeline. Passing tests and clean CI do not
mean these are resolved -- they mean the software runs correctly given its current, unfixed
assumptions.

## Geolocation estimator and detection

### Merged-segment SNR uses `max`, not a weighted mean

`detect/merge.py`, `merge_recordings()`: when segments (or drive-bin windows) covering the same
candidate are merged, `average_snr_db` and `p95_snr_db` of the merged candidate are the **maximum**
across the contributing segments/windows, not a duration- or quality-weighted mean. `measured_center_hz`
and the width fields *are* properly averaged/medianed in the same function; the two SNR fields are
not. Because a live drive bin holds up to 10 windows, and this merged value becomes
`rf_observations.snr_db`/`p95_snr_db` and hence `geo_measurements.level_db`, this is a systematic
upward bias on measured levels that grows with the number of contributing windows. The solver reads
level as distance, so this pulls estimated regions toward "closer than measured."

### Channel SNR is computed as (signal+noise)/noise, not excess/noise

`detect/features.py`, `feature_at()`: `average_snr_db`/`p95_snr_db` are
`10*log10(mean(S+N)/mean(N))`, i.e. total power over noise, not `10*log10((S+N-N)/N)`. The correct
excess quantity (`percentile_excess`) is computed a few lines above and used for centroid/width/
symmetry, but not for the two SNR fields. This compresses the reported SNR near the detection
threshold (a true 0 dB reads as roughly +3 dB) and understates how far below the gate a weak signal
actually is.

### Common-mode correction is not leave-one-out

`geo/commonmode.py`, `estimate_offsets()`: the per-stop offset is the median of residuals across
every site detected at that stop, including the site the offset is then applied to. With the
minimum of 3 sites required to estimate an offset, the median can equal one site's own residual,
shrinking that site's apparent misfit and making the corrected fit look better than it is. Tests pin
median robustness to one bad site and pin that correction does not make things worse by more than
1 km, but nothing pins independence of a site's correction from its own contribution.

### `BinVisit.travelled_m()` measures displacement, not path length

`live/bins.py`: the adaptive-span close condition compares straight-line distance from a visit's
origin to its latest fix against `span_target_m`. On a curved road or a U-turn this understates the
road actually covered, so a visit can stay open past the intended 150 m span (or, on a full U-turn,
never close on this condition at all). In practice `max_windows_per_bin` (10) usually closes the
bin first, which masks this on most drives but does not fix it. `travelled_m()` and `spread_m()`
also use a hard-coded `111_320.0` m/degree rather than the `EARTH_RADIUS_M` haversine used elsewhere
in `geo/model.py`, a ~0.1% inconsistency.

### The bin window cap keeps the first N windows, not a spread sample

`live/session.py`: `max_windows_per_bin` (10) is a hard cap on windows *appended in arrival order*;
once reached, the bin closes immediately and further windows in the same bin are dropped
(`windows_dwelled`) rather than sampled across the remainder of the bin's span. In slow traffic this
means a bin can close, and its measurement be placed, after only the first tens of metres of its
150 m nominal span. The bin's `settings_json.bin_size_m` is written as the nominal 150 m regardless
-- `position_spread_m` is recorded separately and shows the true spread, but nothing currently reads
it to flag a bin as under-spanned.

### GPS accuracy is stored and never used; drive bins do not store it at all

`survey_runs.gps_accuracy_m` and `geo_measurements.position_accuracy_m` exist and are populated for
stationary stops (from the browser's `GeolocationPosition.coords.accuracy`), but `geo/pipeline.py`'s
`GeoMeasurement` construction does not read them, and the solver has no positional-uncertainty term
-- every stop's position is treated as exact regardless of reported accuracy. `live/session.py`'s
`_write_visit()` does not populate `gps_accuracy_m` on drive-bin survey runs at all, so this field is
`NULL` for every live/drive measurement today, independent of whatever accuracy the phone reported.

### No credible-region coverage test exists

`tests/test_geo_solver.py` checks that regions shrink as sessions accumulate and that a noiseless
scenario recovers a known transmitter within 1.5 km; it does not check that a 90% credible region
contains the true transmitter in roughly 90% of independent trials. The one coverage measurement
that exists in the repository (the study behind the 150 m bin-size choice, `live/bins.py`'s module
docstring) is a comment citing a one-off, 20-trials-per-spacing simulation; its own headline result
(50 m -> 60% coverage; 150 m -> 90% coverage) is pinned only as a hard-coded constant check
(`MIN_ADAPTIVE_BIN_M == MAX_ADAPTIVE_BIN_M == 150.0` in `tests/test_live_session.py`), not as a
reproducible, re-runnable calibration test.

## Drive mode plumbing

### The most likely mechanism for a drive bin repeating despite movement

`web/static/app.js`'s `sendFix()` does not forward the browser's `GeolocationPosition.timestamp` to
the server; `web/service.py`'s `push_live_position()` stamps every incoming fix with
`time.monotonic()` on arrival. `LiveSession._fresh()` (`live/session.py`) therefore measures *arrival
latency*, not *fix age* -- if the browser's location provider repeats a stale coordinate (a common
Android/iOS behaviour when the fused location provider has not produced a new fix), each repeated
POST still looks fresh to the server indefinitely, and there is no frozen-fix or GPS-jump detection
anywhere in the pipeline to catch it. This is the most likely explanation for a previously observed
"same bin repeating" symptom; it has not been confirmed with a deterministic replay because no such
replay mechanism exists yet (see below).

### The SDR-reading thread performs FFT/detection inline under backpressure

`live/session.py`: the streaming thread hands each closed bin to a one-deep (`maxsize=1`) queue for
a separate `live-detect` worker thread. When that queue is full, the streaming thread runs the full
detector (`self._detect(visit)`, a band-wide FFT scan) **inline**, on the same thread that is
supposed to keep reading from the SDR, before returning to the read loop. This is an intentional,
tested trade-off (`bins_analysed_inline` is counted; `tests/test_live_session.py` pins that the
capture keeps going rather than dropping a measurement) -- but it means a slow analysis pass can and
does compete with the SDR read loop for CPU time, and the actual overflow cost of this on a
Raspberry Pi has not been measured (the test suite exercises it against a synthetic device stub with
no real timing pressure).

### No deterministic replay of a drive exists

Per-window PSDs and per-fix GPS positions inside a bin are discarded once the bin is written; only
the aggregate (centroid, spread, window count, cell keys) survives in SQLite. The live GPS trail
shown in the web UI is an in-memory ring buffer (`_LIVE_TRAIL_LIMIT = 1200` fixes, roughly 20 minutes
at 1 Hz) that is never persisted to disk. There is no `live replay` command and no stored
device-timestamped GPS trace, so a field anomaly (such as the repeating-bin symptom above) cannot
currently be reproduced offline from what was recorded during the drive that exhibited it.

## Documentation accuracy (fixed in this stabilization branch, listed for the PR record)

- `README.md` and `docs/PHASE7-FIELD-GEOLOCATION.md` described `--min-detections` as defaulting to
  3 and requiring "three detections"; the shipped default is 2, and the actual gate below that is a
  count of independent constraints, `(detections - 1) + non-detections >= 2`. Fixed in this branch.
- `docs/PHASE7-FIELD-GEOLOCATION.md` and `README.md` described `--keep-recordings` as defaulting to
  0 (`README.md`) or 1 (`docs/PHASE7-FIELD-GEOLOCATION.md`) inconsistently; the code default was 0.
  This branch changes the code default to 1 (see "Interim IQ retention policy" below) and aligns
  both documents with it.
- `live/bins.py`'s comment described `DEFAULT_BIN_SIZE_M` (150 m) as being above the ~100-200 m
  suburban shadow-fading decorrelation distance it also documents; 150 m sits inside that range, not
  above it. Fixed in this branch (comment only, no behaviour change).
- The `README.md`/`docs/PHASE7-FIELD-GEOLOCATION.md` example `web serve` commands did not include
  `--tls`, while the surrounding prose already stated that Drive mode requires it. Fixed in this
  branch by adding an explicit `--tls` example labelled for staging/testing use; the field app's
  default bind and TLS behaviour are unchanged.
- `docs/known-limitations.md` described the project as performing "container inspection only",
  which was true at Phase 1 and has not been true since Phase 3. Marked historical in this branch.

## Not yet measured

- **`drive_view_for_stops=True` (default since the branch that introduced it):** every stationary
  stop taken through the field app is now also read back the way a drive bin would hear it (a second
  survey pass, excluded from geolocation), on by default. Its added wall-clock cost, CPU and memory
  impact on a Raspberry Pi have not been measured. The Pi smoke test protocol for this stabilization
  (`docs/validation/pi-smoke-v0.10.md`) measures this and reports a data-based recommendation before
  merge.
