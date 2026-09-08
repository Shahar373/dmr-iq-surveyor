# Pi smoke test — v0.10.0 stabilization

**Status: FAILED/PARTIAL on `9e5dfbe`; not yet rerun on the fix.** See "Field attempt" below for what
was and was not actually exercised. This file's own checklist (§§0-9) is still a template only, empty
because the attempt did not reach most of it; results are filled in, committed, and pushed as a
separate, later step (gate G4b) once the smoke test in gate G4 is actually rerun on the Raspberry Pi
against a commit at or after `3412f6e`. Every field below must be filled with a real, observed value
or explicitly marked "not run" / "not applicable" -- never left as a plausible-looking placeholder.

Do not use `--host 0.0.0.0` for this test. Bind explicitly to a Tailscale or hotspot address (see
§0). See Plan v2, §§8-9, for the staging model this protocol assumes (detached-HEAD clone or
worktree at a known commit; the Pi's existing checkout and `.venv` are never touched).

## Field attempt — operational failure on commit `9e5dfbe`

This section is a record of the one real field attempt made against this protocol, not a full run of
it -- but it was not a total miss against the checklist below either:

| Section | Status |
|---|---|
| §1 Software checks (pytest / ruff / CLI diff) | Done |
| §2 Database upgrade, on a copy | Done |
| §3 Preflight | Done |
| §4 `drive_view_for_stops` replay (plain and `--drive-view`) | Partially completed -- both replay runs and their metrics were done, but the section's table and recommendation below were not fully filled in at the time |
| §5 Field app: stationary stop | Server start and TLS acceptance done; the stop capture itself was not reached |
| §6 Field app: drive | Not done -- never reached |
| §7 After stopping | SDR-release check done; `campaign_digest.py` not run |
| §8 Hardware health | Metrics collected |
| §9 Outcome | fail/partial (this attempt) |

The fix described in this branch (`stabilize/p25-geolocation-v0.10`, commits `b8133d4` through
`3412f6e`) has been exercised only against a stub SDR in the automated test suite and in local
browser smoke tests (see `tests/test_web_device_state.py`, `tests/test_web_bootstrap.py`) --
**it has not yet been field-validated on real hardware.** A PASS on this document requires an
actual re-run of the full protocol below on the Pi, on a commit at or after `3412f6e`.

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
  capture settings loaded and no recording was made. No completed `/api/state` request appears in the
  server log -- consistent with the request hanging before `send_response()`, which is the only place
  `BaseHTTPRequestHandler` logs from (see `web/server.py`'s `log_message`). `GET /api/state` called
  `probe_soapysdr()` synchronously, on the request thread, with no timeout, no cache, and nothing on
  the client side to abort or report it, and that path contained one unbounded call,
  `SoapySDR.Device.enumerate()`. No stack trace was captured during the failure, so it is not
  established as fact that `enumerate()` itself was the call stuck at that moment -- but it is the
  leading suspect: `probe_soapysdr()` reaches `enumerate()` on every call, including the preflight
  that succeeded earlier in this same attempt (a separate, successful run of the same call, not
  evidence that `enumerate()` is safe under whatever condition caused the hang). The fix in this
  branch removes that one unbounded call from the request path regardless of whether it was the
  actual cause -- it does not make the request thread block-proof in general, since `state()` still
  does synchronous SQLite and disk work.
- **Not attempted at the time:** the §6 drive scenario, and the §5 stop-capture itself (server start
  and TLS acceptance were reached, per the table above, but the capture never ran because the app
  never became usable). §7's SDR-release check was done; `campaign_digest.py` was not run.

### Two residual limitations, not fixed here, documented rather than guessed around

1. **`SoapyIqDevice.open()` still has no timeout.** Only the probe/enumerate path (`capture/probe.py`,
   `web/devices.py`) is now bounded and killable. If the SDRplay device opens but then hangs inside
   `open()` itself -- a separate C call from `enumerate()` -- nothing here catches that; it would
   still block the calling thread indefinitely. A thread-based timeout around it was deliberately
   **not** attempted: a stuck C call cannot be safely cancelled from another thread (killing the
   thread does not release whatever the C library is blocked on, and can corrupt shared state). The
   only safe direction identified is running the capture itself in its own killable child process,
   which is a separate, not-yet-written plan and out of scope for this branch.
2. **`DeviceMonitor.close()` is bounded and best-effort against a child stuck in an uninterruptible
   wait (D-state), not a guarantee.** It kills the probe subprocess and waits up to its own bounded
   join window for it to be reaped; if the kernel has the child parked in D-state (typically stuck in
   an uninterruptible driver/USB call), `kill()` cannot remove it, that wait still elapses, and
   `close()` then returns anyway rather than blocking the server further. This is a guarantee about
   the *server* -- normal operation does not wait on such a child past that bounded window -- not
   about the *child*: `close()` never claims the orphaned process itself is guaranteed to be gone.

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
