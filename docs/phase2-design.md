# Phase 2 — streamed spectrum analysis

Phase 2 reads SDRconnect IQ through `IQMemmapReader` and performs overlapping FFTs without loading the full recording into RAM.

## Numerical outputs

- Average and max-hold PSD use linear power accumulation before conversion to dB.
- PSD is normalized by sample rate and window energy and labeled relative dBFS/Hz.
- The percentile spectrum is deterministic and uses evenly distributed FFT frames capped by `percentile_max_frames`.
- Local noise is estimated independently for every FFT using medians over coarse frequency windows and interpolation between windows.
- Occupancy is the percentage of FFT frames whose PSD exceeds the local floor by `occupancy_threshold_db`.
- Waterfall time bins average linear power. Frequency is reduced to a configurable number of bins to keep Raspberry Pi memory and output size bounded.

## Exclusions

Receiver edges and the zero-IF/DC area are marked with boolean columns in every frequency CSV. They remain in the data for auditability.

## Frequency-axis convention

For an even FFT, the first bin center is exactly `center - sample_rate / 2`; the last bin center is one FFT-bin below the nominal upper edge. Reports include both nominal coverage and actual first/last bin centers.
