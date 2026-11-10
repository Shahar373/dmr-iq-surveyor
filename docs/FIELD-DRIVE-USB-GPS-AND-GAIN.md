# Field procedure: a drive with the Pi's own GPS and a calibrated gain

How to locate one continuously transmitting carrier (for example a control channel on
469.350 MHz) by driving past it alone, with a u-blox USB GPS (VK-162) on the Pi and a magnetic
roof antenna. Read [`phase7-geolocation-design.md`](phase7-geolocation-design.md) first for what
a result means: a region is a search-area reduction, not a transmitter coordinate.

**Maturity: experimental.** The estimator has not been validated against a transmitter at a
known position on this band. The first drive should be one where the answer is known.

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

Import the channel you are looking for as reference data. It is matched only after observation
and never changes what the detector looks for. The import format is still the P25 one, so a
non-P25 channel needs placeholder identifiers that say so:

```
wacn_hex,system_id_hex,rfss,site,observation_status,primary_cc_mhz,nac_hex,notes
00000,000,0,1,DIRECT,469.350000,,PLACEHOLDER IDs - DMR Capacity Max CC located by frequency only
```

```bash
dmr-surveyor geo import-sites config/cc469.csv --snapshot-id cc469_v1
```

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

## Known limits

- NMEA RMC/GGA carry no accuracy in metres. HDOP and satellite count are shown; `accuracy_m` is
  left empty rather than invented.
- Calibration judges clipping by the ADC peak at the place it ran. A carrier much stronger than
  anything heard there can still clip later; the result warns when headroom is under 30 dB.
- The calibration cannot compare LNA states without a signal to compare them by. In that case it
  keeps the current LNA state.
