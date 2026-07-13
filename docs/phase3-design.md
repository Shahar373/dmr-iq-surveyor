# Phase 3 — Candidate Detection

Phase 3 consumes the CSV, JSON, and waterfall artifacts produced by Phase 2. It does not reopen or reprocess the original IQ recording.

## Detection approach

The detector scans an absolute 6.25 kHz raster and scores each window using multiple independent features:

- integrated average SNR
- integrated P95 SNR
- occupancy
- 90% occupied width
- equivalent occupied width
- spectral fill ratio
- left/right symmetry
- peak-to-channel-mean ratio

A non-maximum-suppression pass removes overlapping scan windows from the same signal without merging channels 12.5 kHz or 25 kHz apart.

## Preliminary classes

- `dmr_like_narrowband`
- `intermittent_narrowband`
- `narrow_carrier_or_spur`
- `wideband_unknown`
- `noise_or_artifact`

These labels are spectral only. `confirmed_dmr` is intentionally unavailable until decoder evidence is added in a later phase.

## IQ orientation

The current recordings use an assumed `IQ` channel order. Each retained candidate therefore records both the assumed frequency and its mirror around the recording center frequency.

## Passband warnings

The validated approximately flat region for the current 10 MHz recordings is 159.49–167.68 MHz. Candidates outside it remain in the output but receive a passband warning and should be re-recorded closer to the center frequency before strong conclusions are drawn.
