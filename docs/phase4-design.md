# Phase 4 design — narrowband extraction and DSD-FME

Phase 4 converts ranked Phase 3 candidates into decoder-ready discriminator audio while preserving all source, DSP, decoder, and quality provenance.

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
  -> robust median centering
  -> percentile level target capped by a peak-safe scale
  -> mono signed 16-bit PCM WAV with zero clipping
```

The wideband input remains memory mapped and is processed sequentially. FFT overlap-add convolution avoids the cost of direct long-FIR filtering at 10 MS/s. BLAS and OpenMP thread counts are limited by the Raspberry Pi helper script.

## PCM normalization

The normalizer calculates two scale factors:

1. a percentile-derived scale that gives ordinary discriminator samples a useful level;
2. a peak-safe scale that keeps the absolute largest sample within `output_peak_fraction` of PCM16 full scale.

The smaller scale is applied. The extraction report records:

- percentile and absolute-peak references;
- both scale factors and the selected scale;
- whether the peak cap was applied;
- how many samples would have clipped without it;
- actual clipped sample count, which is zero by default;
- final PCM peak.

## Decoder contract

The upstream DSD-FME examples document 48 kHz mono WAV input as:

```bash
dsd-fme -i filename.wav
```

DMR stereo mode is selected with `-fs`; inverted DMR may require `-xr`. The implementation probes `dsd-fme -h`, records the binary path and help-text SHA-256, and tries normal and inverted DMR profiles. `-o null` is added only when the installed help text advertises null output support.

A process return code is not protocol evidence. Decoder output is parsed after ANSI escape sequences are removed.

Only signed lines matching `Sync: +DMR` or `Sync: -DMR` count as explicit DMR sync evidence. Generic lines such as `Sync: DMR | VOICE CACH/EMB ERR` are error diagnostics and do not increase the sync count.

## Slot parsing

DSD-FME prints both slot labels on each status line. The active slot is the bracketed token:

```text
[SLOT1] slot2
slot1 [slot2]
```

Phase 4.1 counts only the bracketed token. It does not count both slots merely because both words are present.

## Evidence tiers

Decoder attempts use three positive evidence tiers:

```text
dmr_sync_only
dmr_confirmed_degraded
dmr_confirmed_clean
```

`dmr_confirmed_clean` requires repeated numeric Color Code evidence, a consistent dominant Color Code, a high valid-CC ratio, multiple clean signed sync lines, and a low error ratio.

`dmr_confirmed_degraded` requires numeric and internally consistent Color Code evidence, but tolerates a higher error rate.

`dmr_sync_only` preserves signed sync-like evidence that does not yet meet confirmation-quality requirements.

## Polarity quality scoring

Normal and inverted attempts are ranked by quality components rather than raw sync count:

- numeric Color Code ratio;
- dominant Color Code consistency;
- clean signed-sync ratio;
- inverse CRC/FEC/CACH/frame-error ratio;
- coherent IDLE/CSBK/DATA activity;
- complete voice-stage diversity;
- bounded sync-count support;
- a penalty for repetitive single-stage `VC1` patterns without control events.

All components are stored in decoder JSON reports. Ties prefer normal polarity, but a genuinely higher-quality inverted attempt can still win.

## Batch behavior

- Candidates are sorted by Phase 3 confidence.
- Candidate class, minimum confidence, rank limit, IQ hypotheses, and decoder execution are configurable.
- A candidate is processed only for source recordings listed in its Phase 3 evidence.
- Each recording and IQ hypothesis has a separate output directory.
- Missing DSD-FME produces `decoder_unavailable`; discriminator extraction still succeeds.
- All attempts, including no-sync and sync-only attempts, remain in the batch report.
- Batch CSV and Markdown reports include quality score, dominant Color Code, valid-CC ratio, dominant-CC consistency, error ratio, and active-slot counts.

## Passive scope

The stage performs offline receive-side analysis only. It contains no transmit, authentication, impersonation, injection, brute-force, or decryption capability.
