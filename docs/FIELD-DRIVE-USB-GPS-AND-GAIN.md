# Field procedure: a drive with the Pi's own GPS and a calibrated gain

How to locate one continuously transmitting carrier (for example a control channel on
469.350 MHz) by driving past it alone, with a u-blox USB GPS (VK-162) on the Pi and a magnetic
roof antenna. Read [`phase7-geolocation-design.md`](phase7-geolocation-design.md) first for what
a result means: a region is a search-area reduction, not a transmitter coordinate.

**Maturity: experimental.** The estimator has not been validated against a transmitter at a
known position on this band. The first drive should be one where the answer is known.

## The Locate page (recommended, since 2026-11-12)

The field app has a page for exactly this workflow, in Hebrew: type the frequency, press
**התחל**, drive. Design: [`phase8-field-app-locate.md`](phase8-field-app-locate.md).

1. **Update the Pi** (once, at home). The service runs from `/opt/dmr-field/app`, not from a
   development checkout, so update that one, as `OPERATIONS.md` ("Update") describes:

   ```bash
   cd /opt/dmr-field/app
   sudo git pull
   sudo /opt/dmr-field/venv/bin/pip install -e .
   sudo ./scripts/install_field_service.sh --site /etc/dmr-field/sites/<your-site>.yaml
   sudo systemctl restart dmr-field.service
   fieldctl status
   ```

   Re-running the installer is what refreshes `fieldctl` itself (`fieldctl url --locate` is
   new). The token, certificate and `field.env` are left alone.

   The map needs Leaflet: from the internet, or a local copy made once with
   `bash scripts/vendor_leaflet.sh` while online. Without it the page works with no map.

2. **Open the page.** `fieldctl url --locate` prints its address with the token. Bookmark it
   on the phone. The classic app is still at the old address (`fieldctl url`), and each page
   links to the other. On a tailnet with only your own devices the token can be turned off
   (`sudo fieldctl auth off --write`, see `OPERATIONS.md`), and the address is then just
   `https://<tailscale address>:8765/locate`.
3. **Type the frequency** (for example `469.350`) and press **התחל**. The first time for a
   frequency, park somewhere the carrier is heard: the page calibrates the gain for about a
   minute, then starts the drive by itself. Later drives on the same frequency resume its
   campaign at the same gain and skip calibration.
4. **Drive.** The screen shows, for your frequency only, whether the last measurement heard
   it and how loud, how many measurements heard it, and the GPS. **מדוד כאן 60 ש׳** takes a
   stationary minute (the button pulses when that is worth doing). **עדכן תוצאה** draws the
   current search area without stopping the drive.
5. **Press עצור.** The result is computed at once: the search area, its size, where a next
   measurement would shrink it most, and any measurement that disagrees with the rest.

What the page sets up by itself, which this document otherwise describes by hand: the
emitter (`geo emitter add`), the tuning (100 kHz below the frequency at 2 MS/s), a band
profile for it, a campaign of its own (`loc-f469_350000`), the gain calibration judged by
your frequency (no placeholder P25 import needed), and the Phase 8 solve. Choices that matter
are under **הגדרות היעד**: continuous or intermittent emission, the channel bandwidth (12.5,
25 or 50 kHz; leave 12.5 if unsure), and a known position for a validation drive.

`fieldctl campaign current` shows that the Locate page moved the service to a target's
campaign; `fieldctl restart` returns it to the configured one.

The rest of this document is the same workflow by hand, from the command line. It still
works, and it is the fallback if the page is unavailable.

## By hand, from the command line

**The answer comes from `geo emitter solve`, not from the map in the classic app.** The
carrier is declared as a Phase 8 *emitter*
([`phase8-fixed-emitter-geolocation.md`](phase8-fixed-emitter-geolocation.md)) and solved from
the command line after the drive (section 3). The classic app measures and stores the drive;
its own map still solves the older Phase 7 way, which simulation shows is overconfident on
drive data (90 % regions holding the transmitter 30–50 % of the time). Treat its region as a
live preview only.

### In short

```text
once, on the Pi     geo emitter add  (what to locate)    + placeholder import (for gain calibration)
parked, per campaign  Calibrate gain on the Drive tab
driving             Start drive -> Stop drive
afterwards          geo emitter solve --campaign <id>    <- the result
```

## What changed for a drive

| Before | Now |
|---|---|
| Positions came only from the phone's browser, so a drive needed HTTPS and a phone screen kept awake | `--gnss-device` reads NMEA from a GNSS receiver on the Pi. While it has a fix it places every window, and a phone fix is ignored rather than interleaved with it. The phone becomes the fallback. |
| Gain was picked by hand (see `PHASE7-FIELD-GEOLOCATION.md` section 1) | **Calibrate gain** on the Drive tab steps the receiver through 40 gain settings in about a minute, with a progress bar and voice prompts, and chooses one. |

Neither changes how a bin is measured, stored or solved.

## 0. Once, on the Pi

### If gpsd already owns the receiver (shared Pi)

A serial port has one reader. When gpsd holds the VK-162 -- for example because another
application on the same Pi reads its GPS through gpsd, or chrony takes the time from it -- read
it **through gpsd** rather than opening the port:

```bash
echo 'FIELD_GNSS_DEVICE=gpsd' | sudo tee -a /etc/dmr-field/field.env.local && fieldctl restart
```

`auto` does the same by itself whenever gpsd is listening on 127.0.0.1:2947. Opening the port
directly next to gpsd fails with `Device or resource busy`, and the status says so and names this
setting. Nothing here stops, reconfigures or competes with gpsd, so its other clients keep working.

### If nothing else uses the receiver

```bash
sudo usermod -aG dialout "$USER"     # the GPS is a serial port owned by dialout; log in again
ls -l /dev/serial/by-id/             # the VK-162 shows up as ...u-blox_7...-if00
```

For a manual run, use the band profile made for this tuning
(`config/bands/uhf_469_narrow.yaml`: one 2 MS/s capture centred at 469.250 MHz, which keeps
469.350 MHz 100 kHz away from the receiver's DC spike):

```bash
dmr-surveyor web serve --host 0.0.0.0 --token auto \
  --band uhf_469_narrow --site mobile \
  --center-frequency 469250000 --sample-rate 2000000 \
  --campaign 2026-11_uhf469_day1 \
  --gnss-device auto --output runs/field
```

For the installed service, set `FIELD_GNSS_DEVICE=auto` (or the `/dev/serial/by-id/...` path) in
`/etc/dmr-field/field.env.local` and run `fieldctl restart`. The unit now also grants `dialout`;
re-run the installer, or add `dialout` to `SupplementaryGroups` by hand on an older install.

### Declare what you are locating (Phase 8)

The examples below use the installed service's database. For a manual `web serve` run, use the
database that run writes to (`runs/inventory/dmr_inventory.sqlite3` unless `--database` says
otherwise). Declaring is reference data: it is matched to the stored measurements only afterwards
and never changes what the detector looks for, so it can be done before or after the drive.

```bash
DB=/var/lib/dmr-field/inventory/dmr_inventory.sqlite3

dmr-surveyor geo emitter add cc469 --frequency-mhz 469.350 \
  --emission continuous --label "469.350 control channel" --database "$DB"
```

- `continuous` is right for a control channel that is always keyed: a place where it was *not*
  heard is evidence too, and an important part of the answer. Use `--emission intermittent` for a
  carrier that comes and goes; its non-detections are kept with the reason `absence_not_evidence`
  and not used, because it may simply have been off.
- The default `--tolerance-hz` is 6250 Hz. Two declared emitters closer than that to one
  observation are both marked `ambiguous` rather than guessed.
- **For a validation drive**, declare a transmitter whose position is published or surveyed, in
  the same tuning, with its position. The position is never used by the solver; it only scores
  the result afterwards:

  ```bash
  dmr-surveyor geo emitter add ref1 --frequency-mhz <MHz> \
    --truth-lat <lat> --truth-lon <lon> --truth-source "<where the position comes from>" \
    --database "$DB"
  dmr-surveyor geo emitter list --database "$DB"
  ```

### Also import it as a placeholder site, for now

The field app's **Calibrate gain** chooses its target channel from the Phase 7 registry, and
the field app's map and *Sites* tab draw only registry sites. Until the field app learns about
emitters, import the same channel there too, or calibration falls back to judging by the noise
floor. The import format is still the P25 one, so a non-P25 channel needs placeholder
identifiers that say so:

```
wacn_hex,system_id_hex,rfss,site,observation_status,primary_cc_mhz,nac_hex,notes
00000,000,0,1,DIRECT,469.350000,,PLACEHOLDER IDs - DMR Capacity Max CC located by frequency only
```

```bash
dmr-surveyor geo import-sites config/cc469.csv --snapshot-id cc469_v1 --database "$DB"
```

This placeholder does not affect the emitter result: `geo emitter solve` reads the stored
observations by frequency and ignores the registry.

## 1. Before driving: parked, once per campaign

1. Use a **new campaign**. A campaign is measured at one gain, and the calibration refuses to
   change the gain of a campaign that already has measurements. It says so instead of applying.
2. Wait for **USB GPS (on the Pi)** on the Drive tab to show a position and satellites. It is
   also announced aloud ("USB GPS has a fix"). A cold receiver can take a minute or more.
3. Park where the carrier is heard, ideally the strongest place you expect to drive through, and
   press **Calibrate gain**. Stay still for about a minute.
   - If the carrier is heard, the chosen setting is the *least sensitive* one whose SNR is within
     1 dB of the best any setting reached. It costs no sensitivity and buys the most headroom for
     the stronger signal near the transmitter.
   - If it is not heard here, the choice falls back to the noise floor at the current LNA state,
     and the result says so. Calibrate again where the carrier is audible if you can.
   - Any setting that clips is never chosen.
4. The result shows what was applied, why, and the three lines to copy into the site profile so
   the setting survives a restart. The full step-by-step evidence is written to
   `<output>/calibration/gain_<time>.json`.

## 2. Driving

Press **Start drive**. No phone location sharing is needed while the USB GPS has a fix. Voice
announces GPS lost and back, every fifth bin, and "worth a stop here". The GPS cell on the HUD
reads **USB GPS** while the Pi's receiver is the one placing the measurements.

Geometry matters more than mileage. In simulation of this exact carrier, 8 stops on one ring
around the transmitter gave no bounded region, while 16 places spread from about 1 km to 10 km,
including far ones where the carrier was not heard, bounded it. Drive towards and away from the
area, not around it.

Press **Stop drive** when done. Every bin is already in the database; stopping only ends the
session.

## 3. After driving: the result

Solve the emitters from the campaign you just drove. It rebuilds the emitter measurements from
the stored observations first, so nothing stale is read, and writes JSON and GeoJSON next to the
other field output:

```bash
dmr-surveyor geo emitter solve --campaign 2026-11_uhf469_day1 \
  --database "$DB" --output-root /var/lib/dmr-field/emitters
```

What it does differently from the in-app map, on purpose:

| | in-app map (Phase 7) | `geo emitter solve` (Phase 8) |
|---|---|---|
| level | `snr_db`: (S+N)/N, maximum over a bin's windows | `excess_snr_db`: S/N, median over the windows that heard it |
| neighbouring bins | each counts as independent | bins within 500 m share their shadow fading and count as less than one each |
| suspect stops | not reported | named, with a request to re-measure there |
| known position | not used | error and containment reported |

How to read the table it prints:

- **status** — `ok` is a bounded region. `unbounded_region` means the 90 % region reaches the
  edge of the analysed area: the data says *roughly where*, not *where*; drive further away on
  the far side. `weak_geometry` means it was heard from one direction only. `insufficient_evidence`
  means too few usable bins. None of them is an error.
- **det/non** — bins where it was heard / bins where it was looked for and not heard. Both count.
- **90 % area** — the size of the search area, in km².
- **vs known position** (validation emitters only) — how far the most likely cell is from the
  known position, and whether the known position is inside the 90 % region. A `NO` here on a
  validation drive is the most important thing a drive can show: write it down with the
  campaign id.
- A line naming **suspect stops** means one or more bins disagree with the rest by more than
  three robust standard deviations. Nothing is removed; re-measure near them.

The GeoJSON under `/var/lib/dmr-field/emitters/reports/emitters_<batch>.geojson` opens in any
GIS viewer (QGIS, for example) without uploading the drive anywhere. The region is a search area, not a transmitter
coordinate, and one transmitter per frequency is assumed.

In open or rural terrain, shadowing decorrelates over longer distances than the 500 m default;
`--shadowing-correlation-m 1000` is the honest choice there and gives a larger region.

## Known limits

- In the classic app, emitter results are a command-line step (section 3); the Locate page
  shows them.
- NMEA RMC/GGA carry no accuracy in metres. HDOP and satellite count are shown; `accuracy_m` is
  left empty rather than invented.
- Calibration judges clipping by the ADC peak at the place it ran. A carrier much stronger than
  anything heard there can still clip later; the result warns when headroom is under 30 dB.
- The calibration cannot compare LNA states without a signal to compare them by. In that case it
  keeps the current LNA state.
