# Pi smoke test — v0.10.0 stabilization

**Status: NOT YET RUN.** This is a template and protocol only. It is committed empty as part of
`stabilize/p25-geolocation-v0.10` (gate G1); results are filled in, committed, and pushed as a
separate, later step (gate G4b) after the smoke test in gate G4 actually runs on the Raspberry Pi.
Every field below must be filled with a real, observed value or explicitly marked "not run" /
"not applicable" -- never left as a plausible-looking placeholder.

Do not use `--host 0.0.0.0` for this test. Bind explicitly to a Tailscale or hotspot address (see
§0). See Plan v2, §§8-9, for the staging model this protocol assumes (detached-HEAD clone or
worktree at a known commit; the Pi's existing checkout and `.venv` are never touched).

## Field attempt — operational failure on commit `9e5dfbe`

**G4 has not passed.** This section is a record of the one real field attempt made against this
protocol, not a run of it: the attempt failed before reaching most of the checklist below, and none
of §§1-9 were completed. The fix described in this branch (`stabilize/p25-geolocation-v0.10`,
commits `b8133d4`..`de893fd`) has been exercised only against a stub SDR in the automated test suite
and in local browser smoke tests (see `tests/test_web_device_state.py`, `tests/test_web_bootstrap.py`)
-- **it has not yet been field-validated on real hardware.** A PASS on this document requires an
actual re-run of the full protocol below on the Pi, on a commit at or after `de893fd`.

- **Commit under test:** `9e5dfbeaf7f49fb35fff3b550a22658b667278fb` (dated 2026-09-06; the branch
  head at the time of the attempt, before any of the fix commits in this document existed).
- **Hardware:** Raspberry Pi 5; SDRplay RSP1A, serial `230405A498`; SoapySDR 0.8.0.
- **Access:** Tailscale-only, `100.90.110.54`, self-signed HTTPS.
- **Experiment settings:** centre 868 200 000 Hz, sample rate 768 000 Hz, duration 30 s, IF gain
  reduction 25 dB, LNA state 2, AGC off, band 867.950-868.450 MHz.
- **What worked:** the server started and stayed up; `GET /`, `/app.css`, `/app.js` all returned 200;
  Tailscale connectivity and the self-signed TLS certificate were both fine on the phone; preflight
  (run separately, before starting the server) reported acceptable throughput (~18 MB/s measured
  against a ~3.1 MB/s requirement) and passed its other checks.
- **What failed:** the app UI never left its initial `connecting…` / `disk…` placeholders. No
  capture settings loaded, no recording was made, and no completed `/api/state` request appears in
  the server log -- consistent with the request hanging before `send_response()`, which is the only
  place `BaseHTTPRequestHandler` logs from (see `web/server.py`'s `log_message`). This matches the
  root cause fixed in this branch: `GET /api/state` called `probe_soapysdr()` synchronously, on the
  request thread, with no timeout, no cache, and nothing on the client side to abort or report it.
- **Not attempted at the time:** because the app never became usable, none of the drive, hold,
  reconnect, or SDR-disconnect scenarios in §§5-7 below were exercised on hardware during this
  attempt.

### Two residual limitations, not fixed here, documented rather than guessed around

1. **`SoapyIqDevice.open()` still has no timeout.** Only the probe/enumerate path (`capture/probe.py`,
   `web/devices.py`) is now bounded and killable. If the SDRplay device opens but then hangs inside
   `open()` itself -- a separate C call from `enumerate()` -- nothing here catches that; it would
   still block the calling thread indefinitely. A thread-based timeout around it was deliberately
   **not** attempted: a stuck C call cannot be safely cancelled from another thread (killing the
   thread does not release whatever the C library is blocked on, and can corrupt shared state). The
   only safe direction identified is running the capture itself in its own killable child process,
   which is a separate, not-yet-written plan and out of scope for this branch.
2. **`DeviceMonitor.close()` is best-effort against a child stuck in an uninterruptible wait
   (D-state).** It kills the probe subprocess and gives it a bounded window to be reaped; if the
   kernel has the child parked in D-state (typically stuck in an uninterruptible driver/USB call),
   `kill()` cannot remove it and `close()` returns without waiting further, so the API/server never
   blocks on it. This is a guarantee about the *server*, not about the *child*: `close()` never
   claims the orphaned process itself is guaranteed to be gone, only that normal operation does not
   wait on it.

## 0. Environment (fill in at run time)

| Field | Value |
|---|---|
| Date | |
| Commit under test (SHA) | |
| Staging method used (B1: shared `.venv` via `PYTHONPATH` / B2: dedicated `.venv`) | |
| Staging path | |
| `--host` used for `web serve` (must not be `0.0.0.0`) | |
| Antenna | |
| Cable | |
| SDR (model, serial) | |
| IF gain reduction / LNA state | |
| Location (approximate, for the record only) | |
| Free disk space before starting (must be >= 8 GB or the computed preflight requirement, whichever is greater) | |

## 1. Software checks

- [ ] `python -m pytest -q` on staging: result = \_\_\_ passed / \_\_\_ skipped / \_\_\_ failed.
      Every skip must be named and explained here:
- [ ] `python -m ruff check .`: result = \_\_\_
- [ ] `dmr-surveyor --help` and each sub-app's `--help` (`survey`, `geo`, `web`, `live`) diffed
      against the pre-stabilization branch head: confirms additive-only (yes/no, paste diff if no)

## 2. Database upgrade (on a copy, never the live database)

- [ ] Copy made at: \_\_\_ (path)
- [ ] Row counts per table, before vs. after opening with the new code (paste output):
- [ ] `PRAGMA table_info(sites)` includes `lna_state`: yes/no
- [ ] `PRAGMA table_info(geo_run_exclusions)` includes `scope`: yes/no
- [ ] `PRAGMA table_info(geo_solutions)` includes `fit_status`: yes/no
- [ ] No row count decreased in any existing table: yes/no

## 3. Preflight

- [ ] `dmr-surveyor survey preflight ...` result (paste): device / free-space / throughput / band
      coverage / rate efficiency / GPS -- pass/warn/fail for each

## 4. `drive_view_for_stops` measurement (see docs/known-issues-v0.10.md, "Not yet measured")

Run the same recording through `survey run` twice: once plain, once with `--drive-view`.

| Metric | Plain | With `--drive-view` |
|---|---|---|
| Wall-clock time | | |
| `elapsed_seconds` (from the report) | | |
| CPU load (`/proc/loadavg`, before/after) | | |
| Peak RSS (if measured) | | |

**Operational delay introduced (yes/no, how many seconds):**

**Data-based recommendation (leave `drive_view_for_stops=True`, or change it, and why):**

## 5. Field app: stationary stop

- [ ] `web serve` started with an explicit, non-`0.0.0.0` `--host` and `--tls`; certificate accepted
      on the test phone
- [ ] Record stop (30 s) completed: capture -> survey -> (drive view) -> measurements -> solve
- [ ] Stage timings from job events (paste):
- [ ] `*_capture_report.json`: overflow_count = \_\_\_, time_coverage = \_\_\_

## 6. Field app: drive

- [ ] Share my location -> Start drive, run for approximately 3 minutes
- [ ] Bins written: \_\_\_ (>= 3 required)
- [ ] One 60 s hold requested and completed: yes/no
- [ ] Stop drive completed cleanly
- [ ] `LiveStats` at the end (paste): bins_written, bins_analysed_inline, windows_dropped /
      windows_without_position, overflow_count, other counters of interest
- [ ] GPS age observed during the drive (approximate range):

## 7. After stopping

- [ ] SDR released: a subsequent `probe_soapysdr` / preflight succeeds without restarting anything
- [ ] `scripts/campaign_digest.py` run against the copy database (paste summary):

## 8. Hardware health (before / after)

| | Before | After |
|---|---|---|
| `vcgencmd get_throttled` | | |
| `vcgencmd measure_temp` | | |
| `free -h` | | |
| `df -h` (staging + database partition) | | |

## 9. Outcome

- [ ] Overall result: pass / pass with notes / fail
- [ ] Anything not tested, and why:
- [ ] Deviations from this protocol, and why:
