# Phase 4 design — narrowband extraction and DSD-FME

Phase 4 converts ranked Phase 3 candidates into decoder-ready discriminator audio while preserving all source and DSP provenance.

## Signal path

```text
10 MHz complex IQ
  -> phase-continuous complex mixer
  -> overlap-save anti-alias FIR
  -> decimate 10:1
  -> second overlap-save anti-alias FIR
  -> decimate 10:1 to 100 kHz complex baseband
  -> 7.5 kHz complex low-pass
  -> FM phase discriminator
  -> rational polyphase resampling to 48 kHz
  -> robust median centering and percentile normalization
  -> mono signed 16-bit PCM WAV
```

The wideband input remains memory mapped and is processed sequentially. FFT overlap-add convolution avoids the cost of direct long-FIR filtering at 10 MS/s. BLAS and OpenMP thread counts are limited by the Raspberry Pi helper script.

## Decoder contract

The upstream DSD-FME examples document 48 kHz mono WAV input as:

```bash
dsd-fme -i filename.wav
```

DMR stereo mode is selected with `-fs`; inverted DMR may require `-xr`. The implementation probes `dsd-fme -h`, records the binary path and help-text SHA-256, and tries normal and inverted DMR profiles. `-o null` is added only when the installed help text advertises null output support.

A nonzero process exit or a zero exit is not considered protocol evidence. `confirmed_dmr` requires at least one explicit `Sync: ... DMR` line in captured output.

## Batch behavior

- Candidates are sorted by Phase 3 confidence.
- Candidate class, minimum confidence, rank limit, IQ hypotheses, and decoder execution are configurable.
- A candidate is processed only for source recordings listed in its Phase 3 evidence.
- Each recording and IQ hypothesis has a separate output directory.
- Missing DSD-FME produces `decoder_unavailable`; discriminator extraction still succeeds.
- All attempts, including no-sync attempts, remain in the batch report.

## Passive scope

The stage performs offline receive-side analysis only. It contains no transmit, authentication, impersonation, injection, brute-force, or decryption capability.
