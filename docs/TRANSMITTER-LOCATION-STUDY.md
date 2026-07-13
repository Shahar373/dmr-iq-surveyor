# DMR IQ Surveyor — transmitter-location study plan

This document describes what can and cannot be inferred from passive multi-location recordings of fixed DMR carriers.

## 1. Is transmitter location estimation possible?

Yes, but the achievable result depends strongly on the measurement method.

A single portable SDR used sequentially at different locations can usually produce:

- a coverage map;
- relative strong/weak regions;
- a coarse probable transmitter area;
- evidence that two frequencies may share similar coverage behavior.

It usually cannot produce a precise transmitter coordinate from RSSI alone.

For high-confidence localization, add at least one of:

- directional bearing measurements from several sites;
- simultaneous time-difference-of-arrival measurements;
- a phase-coherent antenna array.

## 2. Why RSSI-only localization is difficult

Received power is not determined by distance alone.

It is also affected by:

- transmitter power and antenna pattern;
- transmitter height;
- terrain and line of sight;
- buildings and vegetation;
- reflections and multipath;
- receiver antenna orientation and polarization;
- coax loss;
- receiver gain and overload;
- fading over time;
- whether one or several transmitters share the same frequency;
- whether the observed carrier is a repeater, base station or another network element.

A site farther from the transmitter can measure a stronger signal than a nearer site if it has a clear elevated path.

Therefore, an RSSI result should be reported as a probability region or coverage surface, not as an exact point.

## 3. Method A — sequential RSSI coverage mapping

### What it uses

- one receiver;
- one fixed antenna system;
- several locations measured at different times;
- identical manual receiver settings;
- local noise-floor and channel-SNR measurements.

### Minimum campaign design

- 8–12 sites for an initial survey;
- sites distributed around the suspected region, not along one line;
- two repeated captures per site;
- exact GPS coordinates and antenna metadata;
- identical center frequency, sample rate and gain;
- all confirmed carriers measured simultaneously when possible.

### Recommended metric

Prefer a robust channel-to-noise metric:

```text
channel_score_db = integrated_channel_power_db - local_noise_floor_db
```

For each site, retain:

- average SNR;
- P95 SNR;
- occupancy;
- median of two repeats;
- repeat difference;
- passband and overload warnings.

### Basic model

A simplified log-distance model is:

```text
P(d) = P0 - 10 n log10(d / d0)
```

where:

- `P(d)` is received power or relative channel score;
- `P0` is the unknown reference power;
- `n` is the path-loss exponent;
- `d` is distance from a proposed transmitter location.

The transmitter latitude/longitude, `P0` and `n` can be fit jointly with robust loss. The fit is non-linear and can have several plausible solutions.

Do not fix `n` to one universal value. Urban, suburban, hilltop and obstructed paths behave differently.

### Recommended output

- map of measurement sites;
- per-frequency heatmap;
- best-fit transmitter region;
- bootstrap confidence region;
- residual map showing sites that do not fit the distance model;
- warning when the model is not trustworthy.

### Realistic accuracy

Approximate expectations, not guarantees:

- open terrain with strong geometry and stable carrier: several hundred metres may be possible;
- suburban or hilly terrain: hundreds of metres to several kilometres;
- dense urban multipath: the strongest-area estimate may be misleading;
- simulcast or multiple transmitters on one frequency: a single-source estimate may be invalid.

RSSI-only analysis is best treated as a first-pass search-area reduction method.

## 4. Method B — directional bearing intersection

### What it uses

- one receiver;
- a directional VHF antenna such as a Yagi or log-periodic;
- preferably a step attenuator;
- bearings measured from at least three separated sites.

### Procedure at each site

1. Keep receiver gain fixed.
2. Mount the directional antenna at a repeatable height.
3. Rotate through 360 degrees in 10–15 degree steps.
4. Record a stable channel-power metric at each angle.
5. Repeat the rotation in the opposite direction.
6. Record the strongest direction and beam-width uncertainty.
7. Repeat from at least two additional sites.

The intersection of bearing sectors provides a probable source region.

### Advantages

- much less dependent on absolute transmitter power;
- can refine an RSSI heatmap efficiently;
- works with one portable receiver;
- can be performed after the first campaign identifies a smaller search region.

### Failure modes

- reflections can produce a stronger false bearing;
- nearby metal objects distort the antenna pattern;
- excessive receiver gain can flatten the directional peak;
- vertical and horizontal polarization mismatch can bias results;
- broad antenna beam width produces large intersection uncertainty.

### Realistic accuracy

- clear line of sight and careful geometry: tens to a few hundred metres;
- urban or reflected paths: several hundred metres or a wrong intersection;
- only two bearings: ambiguity remains high;
- three or more bearings with different geometry are preferred.

## 5. Method C — simultaneous TDOA

### What it uses

- at least three receivers operating simultaneously;
- accurately synchronized sample timing;
- known receiver coordinates;
- recordings of the same DMR burst;
- cross-correlation or matched-event timing.

A time difference defines a hyperbola of possible transmitter locations. Multiple receiver pairs intersect these hyperbolas.

### Timing requirement

Radio propagation is approximately:

```text
1 microsecond ≈ 300 metres
100 nanoseconds ≈ 30 metres
```

At 10 MS/s, one raw sample is 100 ns. Practical accuracy also depends on clock alignment, oscillator drift, interpolation, SNR and multipath.

### Important limitation

Sequential recordings made by moving one receiver cannot be used for TDOA. The same transmission must be observed at multiple receivers at the same time.

### Hardware considerations

- GPS-disciplined oscillators or reliable 1 PPS timing are preferred;
- ordinary system clocks, NTP or manually synchronized laptops are not sufficient for precise RF TDOA;
- each receiver oscillator frequency error must be measured or corrected;
- recordings must preserve unprocessed IQ and accurate sample order.

### DMR-specific challenge

DMR bursts are short and may occur on either timeslot. Correlation must use the same burst, not merely similar idle traffic from different moments.

### Realistic accuracy

With well-designed synchronized equipment and good geometry, TDOA can outperform RSSI substantially. With unsynchronized consumer SDRs, the result can be unusable.

## 6. Method D — coherent angle of arrival

### What it uses

- a phase-coherent multi-channel SDR;
- an antenna array with known geometry;
- calibration;
- algorithms such as MUSIC or beamforming.

A single RSP1B is not a coherent multi-antenna direction-finding receiver.

AoA can provide a bearing from one site, but VHF multipath and array calibration remain important. A purpose-built coherent system is required.

## 7. Recommended practical strategy for this project

Use a staged hybrid approach.

### Stage 1 — wideband multi-location survey

Capture all eight confirmed frequencies at 8–12 sites using the procedure in `docs/FIELD-RECORDING-GUIDE.md`.

Goal:

- produce per-frequency coverage maps;
- identify strongest and weakest regions;
- detect channels with similar spatial fingerprints;
- identify inconsistent measurements or possible multiple-source behavior.

### Stage 2 — repeat and refine

Choose the most likely region and add 6–10 closer sites with 200–500 m spacing.

Goal:

- determine whether strength changes smoothly;
- narrow the likely area;
- test whether a high site is strong because of line of sight rather than proximity.

### Stage 3 — directional bearings

Use a directional antenna from at least three sites outside the strongest region.

Goal:

- intersect bearing sectors;
- distinguish a true nearby source from a remote hilltop source with good line of sight.

### Stage 4 — TDOA or coherent AoA only if needed

Use synchronized or coherent hardware only when a more precise location is justified.

## 8. First campaign channel strategy

The confirmed channels are:

```text
162.525000 MHz  CC8
162.587500 MHz  CC5
164.300000 MHz  CC7
164.325000 MHz  CC6
164.537500 MHz  CC8
164.725000 MHz  CC7
165.625000 MHz  CC6
167.137500 MHz  CC7
```

A single 10 MS/s IQ recording centered at 164.831250 MHz captures all eight channels simultaneously.

This is preferable for comparison because:

- all channels experience the same receiver gain state;
- all channels are recorded at the same site and moment;
- one capture avoids retuning and antenna movement between channels;
- spatial fingerprints can be compared later.

Do not assume that channels sharing a Color Code share one transmitter. Color Code is not a unique site identifier.

## 9. How to compare channels spatially

For each channel, create a vector of site measurements:

```text
channel A = [site1_snr, site2_snr, ..., siteN_snr]
```

Then compare normalized spatial patterns using correlation or distance metrics.

High spatial similarity may suggest:

- co-located transmitters;
- shared tower or nearby sites;
- similar antenna height and coverage;
- common propagation conditions.

It does not prove co-location.

Differences may result from:

- different transmitter sites;
- different powers or antenna patterns;
- intermittent traffic and occupancy;
- receiver passband variation;
- local interference near one frequency.

## 10. Data-quality rejection rules

Exclude or flag a site/channel measurement when:

- AGC was enabled;
- manual gain differs from the campaign standard;
- antenna or coax changed;
- antenna height is unknown;
- receiver overload is suspected;
- the channel lies in a flagged passband region;
- the two repeats differ excessively;
- GPS accuracy is poor;
- undervoltage or throttling occurred;
- the file is incomplete or its metadata is inconsistent.

Never hide rejected points. Preserve them with a reason.

## 11. Interpreting a likely transmitter area

A convincing result should show agreement between several independent clues:

- high relative SNR at nearby sites;
- a plausible smooth spatial gradient;
- directional bearings that intersect the same area;
- repeat measurements that remain stable;
- terrain and line-of-sight consistency;
- no evidence of multiple simultaneous sources.

A single very strong point is not enough.

## 12. Ethical and operational constraints

- keep the project passive and receive-only;
- do not attempt access, impersonation or transmission;
- do not enter restricted property;
- do not present an RSSI maximum as an exact transmitter coordinate;
- do not publish precise coordinates of sensitive infrastructure without strong validation and a legitimate purpose;
- report uncertainty and alternative explanations.
