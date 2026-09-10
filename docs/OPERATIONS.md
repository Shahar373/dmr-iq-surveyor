# Operating the field app as a service

The field app used to be started by hand and kept alive with `nohup`. After a
reboot it was gone, and bringing it back meant an SSH session from a phone.
This document covers the packaged alternative: a `systemd` service that starts
at boot, waits for Tailscale, binds only the tailnet address, and keeps a token
that does not change.

Everything here is one-time setup plus five commands. If you only want the five
commands, skip to [Daily use](#daily-use).

## What gets installed where

| Path | What it is |
|---|---|
| `/opt/dmr-field/app` | The deployment checkout. Nothing else uses it. |
| `/opt/dmr-field/venv` | Its virtualenv, with the SoapySDR bindings linked in. |
| `/etc/dmr-field/field.env` | Configuration. No secrets — `systemctl show` can read it. |
| `/etc/dmr-field/token` | The shared API token, mode `0600`. Never in Git. |
| `/etc/dmr-field/sites/…yaml` | Your site profile. Never in Git. |
| `/var/lib/dmr-field` | Recordings, reports, the database, `position.json`. |
| `/var/lib/dmr-field/tls` | The pinned certificate and key. |
| `/usr/local/bin/fieldctl` | The command you actually use. |
| `/etc/systemd/system/dmr-field.service` | The unit. |

This is a **new, separate checkout**. It is deliberately not
`~/Projects/dmr-iq-surveyor` (your working copy) and not
`~/Projects/dmr-iq-surveyor-stage` (the staging clone): a permanent service
must not live in a tree that an unrelated `git checkout` can change underneath
it. The installer refuses to deploy into either.

## Install

Run this at home, on mains power and a real network — not in the field.

### 1. Create the deployment checkout

```bash
sudo mkdir -p /opt/dmr-field
sudo git clone https://github.com/Shahar373/dmr-iq-surveyor.git /opt/dmr-field/app
sudo python3 -m venv /opt/dmr-field/venv
sudo /opt/dmr-field/venv/bin/pip install -e /opt/dmr-field/app
```

### 2. Make SoapySDR importable from that virtualenv

A virtualenv created without `--system-site-packages` cannot see Debian's
`python3-soapysdr`. Skip this and the service starts, serves the UI, and fails
every capture — with nothing in the startup path saying why. The installer
refuses to continue until it passes.

```bash
sudo VENV=/opt/dmr-field/venv bash /opt/dmr-field/app/scripts/pi_soapysdr_setup.sh
/opt/dmr-field/venv/bin/python -c 'import SoapySDR; print("ok")'
```

`VENV=` is what points the script at the deployment's virtualenv. Without it
the script works on the checkout's own `.venv`, which is the right default for
a batch-analysis install and the wrong one here — the service runs from
`/opt/dmr-field/venv`, not from anything inside `/opt/dmr-field/app`.

### 3. Put your site profile somewhere outside the checkout

The site profile records the antenna, receiver and fixed gain. It is
campaign-specific and stays out of Git.

```bash
sudo install -d -m 0755 /etc/dmr-field/sites
sudo cp /opt/dmr-field/app/config/sites/home.example.yaml /etc/dmr-field/sites/<your-site>.yaml
sudoedit /etc/dmr-field/sites/<your-site>.yaml   # record the real antenna, receiver, gain, lna_state
```

There is no default. `web serve` reads its capture gain from this file, so a
guessed one records every stop against the wrong equipment context.

### 4. Choose the capture settings with preflight, not by guessing

The service passes the capture settings explicitly on every start, so they are
visible in `fieldctl status` rather than left to the CLI's defaults. Those
defaults are 5 MS/s for 90 s — 1.68 GiB per stop — and storage that cannot
sustain that write rate **drops samples rather than refusing**, so the failure
arrives as a quietly damaged recording rather than an error.

Measure this Pi's storage first:

```bash
/opt/dmr-field/venv/bin/dmr-surveyor survey preflight /var/lib/dmr-field/recordings \
  --band <band> --sample-rate <rate> --duration <seconds>
```

It reports the highest sample rate the storage can actually sustain. Pass what
you settle on to the installer.

### 5. Read what the installer would do, then do it

```bash
cd /opt/dmr-field/app
./scripts/install_field_service.sh --dry-run \
  --site /etc/dmr-field/sites/<your-site>.yaml \
  --band <name or absolute path> \
  --center-frequency <Hz> --sample-rate <samples/s> --duration <seconds> \
  --driver sdrplay --solve-resolution-m <metres>
```

Then the same command with `sudo` and without `--dry-run`.

Every capture flag is optional; omitting one keeps the value documented in
`deploy/field.env.example` rather than blanking it. They can also be edited
later in `/etc/dmr-field/field.env` followed by `fieldctl restart`.

| Flag | Variable | Passed to `web serve` as |
|---|---|---|
| `--project` | `FIELD_PROJECT` | `--project`, **only when set** |
| `--campaign` | `FIELD_CAMPAIGN` | `--campaign`, **only when set** |
| `--band` | `FIELD_BAND` | `--band` |
| `--center-frequency` | `FIELD_CENTER_FREQUENCY` | `--center-frequency` |
| `--sample-rate` | `FIELD_SAMPLE_RATE` | `--sample-rate` |
| `--duration` | `FIELD_DURATION` | `--duration` |
| `--driver` | `FIELD_DRIVER` | `--driver` |
| `--solve-resolution-m` | `FIELD_SOLVE_RESOLUTION_M` | `--solve-resolution-m` |

IF gain reduction and LNA state are **not** settable here. They come from the
site profile, which is where the field guide puts them, and the app prints at
startup which source it used. A second place to set them would be a second
thing to keep in step.

A campaign-specific band profile belongs outside the checkout, named by
absolute path, the same way the site profile is.

`--dry-run` prints every action and changes nothing; it calls neither `sudo`
nor `systemctl` nor `openssl`. Run it first.

The real run generates the token once, issues the TLS pair once, writes
`/etc/dmr-field/field.env`, installs `fieldctl` and the unit, and enables the
service at boot. It is safe to re-run: an existing token and an existing
certificate are kept, never replaced.

### 6. Start it and look

```bash
sudo systemctl start dmr-field.service
fieldctl status
fieldctl url          # the bookmark for the phone
```

Open that URL on the phone once, accept the certificate warning, and bookmark
it. That warning is expected: the certificate is self-signed.

## Daily use

```bash
fieldctl status        # is it up, where is it bound, is the SDR there
fieldctl logs          # last 50 journal lines
fieldctl logs -f       # follow
fieldctl restart       # warns first if a job is running
fieldctl stop
fieldctl start
```

After a reboot you should need none of them. The Pi boots, waits for the
tailnet, and the bookmark works.

## Update

```bash
cd /opt/dmr-field/app
sudo git pull
sudo /opt/dmr-field/venv/bin/pip install -e .
sudo ./scripts/install_field_service.sh --site /etc/dmr-field/sites/<your-site>.yaml
sudo systemctl restart dmr-field.service
fieldctl status
```

Re-running the installer refreshes `fieldctl` and the unit. Your token,
certificate and `field.env` are left alone, so the phone bookmark keeps
working. That also means new variables are never added to an existing
`field.env` -- `FIELD_PROJECT` and `FIELD_CAMPAIGN` included. Add them by
hand if you want them; leaving them out keeps the service behaving exactly as
it did before projects existed.

## Troubleshooting

Start with `fieldctl status`. It prints the unit state, the address it resolved,
whether anything is actually listening there, the token file's mode (never its
value), the certificate's expiry, and the last few log lines.

**The service is `activating` for a while after boot.** Expected. It waits up
to 90 seconds for a Tailscale address before failing, and systemd retries. A Pi
whose tailnet is slow will come up a minute or two late rather than binding the
wrong thing.

**`no Tailscale IPv4 address after 90s; systemd will retry`.** Tailscale is
down or logged out. `sudo tailscale status`, then `sudo tailscale up`. The
service binds correctly on its next retry — you do not need to restart it. If
there is no tailnet at all and you need the app now, set `FIELD_HOST` to the
hotspot address in `/etc/dmr-field/field.env.local` and restart.

**`start-limit-hit`.** Five failures inside five minutes, so systemd stopped
trying. That is a fast, repeating failure — an unreadable token file, a site
profile that does not resolve — not a slow tailnet. Read `fieldctl logs -n 50`,
fix the cause, then `sudo systemctl reset-failed dmr-field.service` and start it.

**Recordings are damaged, or a stop reports dropped samples.** The sample
rate is beyond what this storage sustains. Re-run `survey preflight` (step 4),
then lower `FIELD_SAMPLE_RATE` or `FIELD_DURATION` in `/etc/dmr-field/field.env`
and `fieldctl restart`. `fieldctl status` shows the values in effect.

**The installer stopped partway.** Nothing is half-installed: it does every
check before the first step that changes anything, and the token, the
certificate and `field.env` are each created once and then left alone. Fix
what it reported and run the same command again. If it stopped at the
SoapySDR check, run step 2 — with `VENV=` naming the deployment virtualenv,
not the checkout's — and then re-run the installer.

**The app loads but every capture fails.** Almost always the virtualenv:
`/opt/dmr-field/venv/bin/python -c 'import SoapySDR'`. If that fails, re-run
step 2. If it succeeds, check the SDRplay daemon with
`sudo systemctl restart sdrplay` and then **Rescan SDR** in the app.

**`/api/state` shows no SDR.** Unplug and replug the RSP1A, then press
**Rescan SDR** in the app. It recovers without restarting the service.

**The phone cannot reach it.** Check the phone is on the tailnet, then
`fieldctl status` — if `listen` says `NO`, the service is not up; if `bind`
says `UNRESOLVED`, Tailscale is down on the Pi.

**The phone shows a certificate warning again.** The certificate was replaced.
That should not happen on its own; see the note below.

## Serving one project and one collection round

Optional, off by default, and additive: an install that sets neither behaves
exactly as it did before projects existed.

```
FIELD_PROJECT=/etc/dmr-field/projects/p25/project.yaml
FIELD_CAMPAIGN=2026-09_day1
```

`FIELD_PROJECT` binds the service to **one** database -- the one its manifest
names, which must already exist and already carry that project's claim. The
process will open no other, and a missing, empty, unclaimed or foreign
database fails at startup rather than being created. Adopt an existing
database once, by hand, before setting this:

```bash
dmr-surveyor project init --adopt \
  --project-id p25_central_il --label "P25 central Israel" \
  --database /var/lib/dmr-field/inventory/dmr_inventory.sqlite3 \
  --manifest /etc/dmr-field/projects/p25/project.yaml
# then, once the report reads right, add --write
```

`FIELD_CAMPAIGN` is the collection round every stop is recorded under. It
selects `<project root>/campaigns/<id>.yaml`, which must already exist, and
it is **only read alongside `FIELD_PROJECT`** -- set on its own it does
nothing and every stop comes back unassigned. `fieldctl status` says so
rather than leaving it to be discovered after a day of driving.

### A contradicting band stops the service, on purpose

`fieldctl` passes `--band`, `--site`, `--database` and `--output` explicitly
on every start. With `FIELD_PROJECT` set, the manifest is a second source for
those, and the precedence is **explicit flag → campaign manifest → project
manifest → the CLI's own default**.

For `--band` a contradiction is refused rather than resolved: levels recorded
under different bands are not comparable, so the service **will not start**,
and systemd will retry and eventually park it in `start-limit-hit`. That is
the intended behaviour -- failing loudly beats recording a day of stops under
a band nobody chose -- but it means `FIELD_BAND` and the manifest's band have
to agree. Site and output simply win from the environment.

Check before restarting, not after:

```bash
sudo -u shahar fieldctl print-command   # the exact argv, including --project
sudo -u shahar fieldctl status          # project, campaign, band, capture
```

`fieldctl restart` then applies it.

### Gain, and the hardware profile

`FIELD_*` still has no gain setting. Gain comes from the site profile named by
`FIELD_SITE`, or -- when the campaign manifest names one -- from a hardware
profile under `config/hardware/`, which outranks the site profile because gain
belongs to the radio rather than to the place. The startup banner always
prints which of the two it used, and falls back to a built-in default only
when neither says anything, saying so when it does.

## Security notes

- **The token is in the URL.** That is how the app bootstraps: static assets are
  served unauthenticated so the page can load and read the token out of its own
  address. So the token is in the phone's browser history, and anyone holding
  the unlocked phone can read it. The server sends `Referrer-Policy:
  no-referrer`, so it is not leaked to the map tile server, and the service
  never puts it in its command line or in the journal — but `fieldctl url`
  prints it by design. Do not paste that URL into a log, an issue or a chat.
- **To rotate the token:** `sudo rm /etc/dmr-field/token`, re-run the installer,
  restart. Every existing bookmark stops working, which is the point.
- **The API can start a capture,** which is why the service binds the tailnet
  address only. `FIELD_HOST=0.0.0.0` is accepted but opens it to every network
  the Pi is on; it is never a default and never a fallback.

## About the TLS certificate

The certificate is self-signed, so the first visit from each device shows a
browser warning: **Advanced → Proceed**. After that the page is a secure
context and the browser will share GPS, which Drive mode requires.

The service does not swap that certificate. It is issued once, at install time,
and the unit points at the resulting file with `--tls-cert`/`--tls-key`, so a
reboot, a change of LAN address, or moving between networks leaves the
certificate and its fingerprint exactly as they were, and the exception your
phone already stored keeps applying.

This is why `--tls` is not used here. That flag re-derives the names it wants on
every start and folds in the machine's current default-route address, so a Pi
that changed networks would be issued a fresh certificate with a fresh
fingerprint — and every phone that had accepted the old one would be sent back
to the warning page.

Two consequences worth knowing:

- The certificate is valid for 397 days and **nothing renews it**. `fieldctl
  status` warns inside 30 days. Reissuing (delete the pair and re-run the
  installer) changes the fingerprint, so every device has to accept it once
  more.
- It covers the addresses the Pi had at install time. Reaching the app by a name
  or address that was not covered will produce a name-mismatch warning; reissue
  if your tailnet name changes.

## Uninstall

```bash
sudo /opt/dmr-field/app/scripts/install_field_service.sh --uninstall
```

That disables and removes the unit and `/usr/local/bin/fieldctl`, and leaves
`/etc/dmr-field` and `/var/lib/dmr-field` — your token, certificate, database
and recordings — alone. To remove those too:

```bash
sudo /opt/dmr-field/app/scripts/install_field_service.sh --uninstall --purge
```

`--purge` deletes recordings and the inventory database. Copy anything you want
off the Pi first.

## Validation

`docs/validation/ops-field-service-acceptance.md` records the acceptance run
on the Raspberry Pi: a full reboot, the service coming back on its own, the
same bookmark and token still working, and a capture completing with no
overflows. It also records the two defects that run found before it passed.

The application itself — capture, survey, device recovery, the geolocation
solve — is validated separately in `docs/validation/pi-smoke-v0.10.md`.

## What this layer does not do

- It does not change the pipeline. `fieldctl exec` runs `dmr-surveyor web
  serve`; every command still works exactly as it did by hand, and
  `scripts/run_field_app.sh` is untouched.
- `fieldctl restart` abandons a running capture. It warns and waits five
  seconds; it cannot resume one.
- Stopping the service sends `SIGINT` so the server can release the SDR, but a
  capture wedged inside a driver call can outlive it. `TimeoutStopSec=30`, then
  `SIGKILL`. See `docs/validation/pi-smoke-v0.10.md` for the underlying
  limitation.
- Nothing here is tested against real hardware by CI. The automated tests never
  run `sudo`, never touch systemd and never open an SDR.
