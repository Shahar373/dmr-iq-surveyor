# Pi smoke test — v0.10.0 stabilization

**Status: NOT YET RUN.** This is a template and protocol only. It is committed empty as part of
`stabilize/p25-geolocation-v0.10` (gate G1); results are filled in, committed, and pushed as a
separate, later step (gate G4b) after the smoke test in gate G4 actually runs on the Raspberry Pi.
Every field below must be filled with a real, observed value or explicitly marked "not run" /
"not applicable" -- never left as a plausible-looking placeholder.

Do not use `--host 0.0.0.0` for this test. Bind explicitly to a Tailscale or hotspot address (see
§0). See Plan v2, §§8-9, for the staging model this protocol assumes (detached-HEAD clone or
worktree at a known commit; the Pi's existing checkout and `.venv` are never touched).

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
| SDR | RSP1B, serial: |
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
