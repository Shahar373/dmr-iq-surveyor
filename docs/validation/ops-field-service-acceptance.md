# Field service acceptance — reboot on the Pi

**Status: PASS on `fc161cd6ee83ed48b0e61781b0a230d9357b004f`, 2026-09-10.**

This is the acceptance run for the operations layer: the `systemd` unit,
`fieldctl`, the installer and the stable token (PR #30, `docs/OPERATIONS.md`).
It answers one question — does the Pi bring the field app back by itself after
a power cycle, reachable from a phone, without anyone opening a terminal — and
the answer was yes.

Run by the operator on the Pi. Recorded here from the operator's report and
the artifacts it names; **this session had no access to the Pi and did not
observe any of it directly.** No GPS values are recorded here, by instruction,
and neither the API token nor the TLS private key appears anywhere in this
file.

This is a separate record from `pi-smoke-v0.10.md`, which validated the
application (capture, survey, device recovery) on `f33f69b`. That protocol is
not re-run here; what is new is everything between power-on and the app being
usable.

## Two defects the Pi found before it passed

Neither was visible to the automated suite, and both were fixed in their own
commits before the passing run. They are recorded because the value of an
acceptance run on real hardware is precisely the things a test on a laptop
cannot see.

1. **The capture settings never reached the service** (fixed in `d9dbfb5`).
   `fieldctl`'s launch command passed neither the centre frequency, the sample
   rate, the duration, the driver nor the solve resolution, so the service ran
   on the CLI's defaults of 5 MS/s for 90 s — 1.68 GiB per stop, and a rate
   already known on this hardware to outrun what the storage sustains. Storage
   that cannot keep up drops samples rather than refusing, so this would have
   arrived as a quietly damaged recording rather than an error. The five
   settings are now read from the environment file, always passed, reported by
   `fieldctl status`, and settable at install time.

2. **`pi_soapysdr_setup.sh` ignored an explicit `VENV`** (fixed in `fc161cd`).
   The installer and `OPERATIONS.md` both told the operator to run it with
   `VENV=/opt/dmr-field/venv`, but the script hardcoded the checkout's own
   `.venv`, so it looked in `/opt/dmr-field/app/.venv` — a virtualenv nothing
   runs from — and the install stopped at step 4. The contract was asserted by
   the installer and the guide before the script offered it.

## Boot and service state

| Check | Observed |
|---|---|
| Unit enabled at boot | `enabled` |
| Unit state after reboot | `active` |
| `NRestarts` | `0` |
| SSH needed to bring the app back | none — it came back on its own |

`NRestarts=0` is the interesting one: the service reached its bind address on
the first attempt, so the Tailscale wait and the ordering did their job rather
than being papered over by the restart policy.

## Reachability from the phone

| Check | Observed |
|---|---|
| Bind address | `100.90.110.54:8765` |
| Bound to `0.0.0.0` | no |
| Same bookmark as before the reboot | worked |
| Same token as before the reboot | worked |
| New certificate warning on the phone | none |

The token surviving a reboot is the whole point of `--token-file`: with
`--token auto` every restart minted a new one and every bookmark died.

## Credentials and TLS

| Check | Observed |
|---|---|
| Token file mode / owner | `0600`, `shahar` |
| TLS private key mode / owner | `0600`, `shahar` |
| Token occurrences in this boot's journal | **0** |
| Certificate valid until | 11 October 2027 |
| Fingerprint across the reboot | unchanged |

The certificate is self-signed, so each device still accepts it once. What
this run confirms is the narrower guarantee the design actually makes: the
service does not swap the certificate or its fingerprint across a reboot, so
the exception the phone had already stored kept applying.

## Receiver

| Check | Observed |
|---|---|
| Device state | `available` |
| Resolved device | `SDRplay Dev0 RSP1A 230405A498` |

## Capture settings in effect

Read back from the running service, not from the file that configures it:

| Setting | Value |
|---|---|
| Centre frequency | 868.2 MHz |
| Sample rate | 768 kS/s |
| Duration | 30 s |
| IF gain reduction | 25 |
| LNA state | 2 |
| AGC | off |

IFGR and LNA came from the site profile, which is where they belong; the rest
came from `/etc/dmr-field/field.env`. This is the table that would have read
5 MS/s / 90 s before `d9dbfb5`.

## Capture `ops_reboot_01`

| Field | Value |
|---|---|
| Frames | 23,040,000 / 23,040,000 |
| `complete` | `true` |
| `timed_out` | `false` |
| `overflow_count` | `0` |
| `device_close_error` | `null` |
| WAV size | 92,160,252 bytes |

`overflow_count=0` over a full 30 s at 768 kS/s is the direct evidence that
the chosen rate is within what this storage sustains — the failure mode that
`d9dbfb5` exists to make visible.

## Database

| Check | Observed |
|---|---|
| Main run | `ok` |
| Drive-view pass | `ok` |
| `PRAGMA integrity_check` | `ok` |

## Hardware health

| Check | Observed |
|---|---|
| Server thread count | 8 |
| Throttling | none |
| Temperature | 47.2 °C |
| Free disk | 9.0 GiB (68% used) |

## Software checks at this commit

`556 passed, 1 expected skip` on Python 3.11 and on Python 3.13, with
`ruff check .` clean on both. The skip is the pre-existing
`DMR_SURVEYOR_TEST_RECORDING` one, which needs a local recording that is not
in the repository.

## Not covered by this record

- The application protocol in `pi-smoke-v0.10.md` was not re-run. Device
  unplug/replug recovery, drive mode and the geolocation solve are recorded
  there, against `f33f69b`.
- No GPS values, by instruction.
- The 397-day certificate has no renewal path that preserves the phone's
  stored exception; reissuing changes the fingerprint. Nothing here tests
  what happens at expiry.
- `PrivateTmp=yes` was not enabled or tried. It is left off because the
  SDRplay API's client handoff has not been checked against it.
- The start-limit behaviour under repeated failure was not induced on this
  run. `NRestarts=0` says the service did not need it, not that the limit was
  exercised.
