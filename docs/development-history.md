# DMR IQ Surveyor — development history

This document is a chronological engineering summary of the project work completed in the ChatGPT development session on 13 July 2026. It is not a verbatim transcript. It records the input data, design decisions, implementation phases, validation results, discovered limitations, and the GitHub workflow used to build the repository.

## 1. Project objective

The project began from a clean repository with the goal of surveying wideband SDRconnect IQ recordings made with an SDRplay receiver and a Raspberry Pi. The pipeline was designed to:

1. validate recording metadata and IQ integrity;
2. create reproducible wideband spectrum products;
3. detect and rank narrowband signals that resemble DMR channels;
4. extract decoder-ready discriminator audio from selected candidates;
5. run DSD-FME offline and preserve all decoder evidence;
6. avoid loading complete multi-hundred-megabyte IQ recordings into RAM.

The project is passive and receive-only. It contains no transmit, injection, impersonation, authentication bypass, brute-force, or decryption capability.

## 2. Source recordings

Two SDRconnect recordings were used throughout the development process:

```text
/home/shahar/Documents/SDRconnect_IQ_20260713_150242_163671500HZ.wav
/home/shahar/Documents/SDRconnect_IQ_20260713_150256_163671500HZ.wav
```

Validated properties:

| Property | Recording 1 | Recording 2 |
|---|---:|---:|
| Center frequency | 163.671500 MHz | 163.671500 MHz |
| Sample rate | 10,000,000 samples/s | 10,000,000 samples/s |
| Encoding | signed 16-bit IQ PCM | signed 16-bit IQ PCM |
| Duration | 6.800109 s | 11.200179 s |
| Nominal coverage | 158.671500–168.671500 MHz | 158.671500–168.671500 MHz |

The files are separate captures with an approximately 7.2-second gap. They are always analyzed independently and are never concatenated into a false continuous timeline.

## 3. Phase 1 — recording inspection

Phase 1 implemented:

- RIFF and RF64 parsing;
- `ds64`, `fmt `, and SDRplay-style `auxi` metadata support;
- center-frequency fallback from SDRconnect filenames;
- memory-mapped IQ reading;
- bounded statistics and diagnostic plots;
- clipping, zero-region, and data-integrity checks;
- batch consistency reports.

### Phase 1 findings

Both recordings were valid for further processing. The two recurring warnings were understood and retained for provenance:

1. the center frequency was absent from container metadata and was derived from the `_163671500HZ` filename suffix;
2. the declared data length exceeded the available data by 36 bytes, consistent with an SDRconnect RIFF/JUNK header-size quirk rather than lost IQ frames.

No clipping or zero-filled regions were found. I/Q balance and correlation were suitable for spectral work. The assumed channel order remained `IQ`; statistics alone cannot prove `IQ` versus `QI` orientation.

### Git history

- PR #2 fixed the initial metadata/reporting edge cases and completed the Phase 1 baseline.

## 4. Phase 2 — streamed spectrum analysis

Phase 2 added a Raspberry Pi-oriented spectral engine:

- 65,536-point Hann FFTs;
- 50% overlap;
- approximately 152.588 Hz frequency resolution;
- average PSD;
- max-hold PSD;
- deterministic P95 PSD;
- adaptive local noise-floor estimates;
- per-bin occupancy;
- reduced 500 × 8,192 waterfall products;
- independent batch processing and combined plots.

### Phase 2 runtime results

| Recording | FFT count | Runtime | Peak RSS |
|---|---:|---:|---:|
| 20260713_150242 | 2,074 | 28.06 s | ~741 MB |
| 20260713_150256 | 3,417 | 46.51 s | ~969 MB |

The implementation remained bounded and practical on the Raspberry Pi. The effective flat passband was narrower than the nominal 10 MHz coverage; approximately 159.49–167.68 MHz was treated as the higher-confidence region.

### Git history

- PR #4 added Phase 2 and was merged after GitHub Actions passed.

## 5. Phase 3 — candidate detection and ranking

Phase 3 converted the spectral products into a ranked frequency inventory. Detection used multiple features rather than a simple peak threshold:

- integrated average and P95 SNR;
- occupancy;
- 90% occupied width;
- equivalent occupied width;
- spectral fill ratio;
- symmetry;
- peak concentration;
- persistence across recordings;
- proximity to 6.25 kHz and 12.5 kHz rasters;
- DC, edge, and passband warnings.

The detector retained 28 findings:

- 22 `dmr_like_narrowband` candidates;
- 2 `wideband_unknown` signals;
- 4 `narrow_carrier_or_spur` signals.

Strong initial priorities included:

```text
165.625000 MHz
164.725000 MHz
164.300000 MHz
164.325000 MHz
162.587500 MHz
164.537500 MHz
167.137500 MHz
164.637500 MHz
```

Every candidate retained both the frequency under the `IQ` assumption and the mirrored frequency that would apply under `QI` orientation.

### Git history

- PR #6 added Phase 3 and was merged after the full test and Ruff workflow passed.

## 6. Phase 4 — narrowband extraction and DSD-FME

Phase 4 implemented the receive-side DSP and decoder pipeline:

```text
10 MHz complex IQ
  -> phase-continuous complex mixer
  -> FIR anti-alias filtering
  -> decimate 10:1
  -> second FIR stage
  -> decimate 10:1 to 100 kHz complex baseband
  -> 7.5 kHz complex channel low-pass
  -> FM phase discriminator
  -> rational resampling to 48 kHz
  -> mono signed 16-bit PCM WAV
  -> DSD-FME normal and inverted profiles
```

The source files remain memory mapped. Candidate/recording combinations are processed sequentially, and the Raspberry Pi helper limits numerical libraries to one thread while using reduced CPU and I/O priority.

Phase 4 added:

- `extract-channel`;
- `decode-channel`;
- `decode-batch`;
- complete DSP provenance;
- DSD-FME binary/help fingerprinting;
- bounded decoder timeouts;
- stdout/stderr preservation;
- conservative extraction of Color Codes, slots, Talkgroups, and Radio IDs.

### First real decoder results

The first batch processed 15 candidate/recording attempts. DSD-FME produced real DMR evidence, but the first report exposed three quality bugs:

1. polarity was selected by raw sync-line count, causing noisy `inverted` attempts to beat coherent `normal` attempts;
2. slot counts searched for the words `slot1` and `slot2`, although DSD-FME prints both labels on every status line;
3. percentile normalization allowed rare PCM16 outliers to clip.

The raw logs showed that normal polarity had stable numeric Color Codes and coherent IDLE/CSBK/DATA/VOICE sequences, while many inverted lines contained `Color Code=XX`, repeated `VC1`, CRC/FEC/CACH errors, and frame-sync errors.

### Validated DMR inventory

After reviewing the normal-polarity logs, the following receive frequencies and Color Codes were established from the short captures:

| Frequency | Dominant Color Code | Observed activity | Recordings |
|---:|---:|---|---:|
| 162.525000 MHz | 8 | CSBK / data | 1 |
| 162.587500 MHz | 5 | CSBK / data | 2 |
| 164.300000 MHz | 7 | mostly idle | 2 |
| 164.325000 MHz | 6 | mostly idle | 2 |
| 164.537500 MHz | 8 | idle and Group Voice | 2 |
| 164.725000 MHz | 7 | idle / data | 2 |
| 165.625000 MHz | 6 | idle / data, degraded in one capture | 2 |
| 167.137500 MHz | 7 | idle / data | 2 |

No reliable Talkgroup or Radio IDs were recovered from these short recordings. The absence of IDs is not evidence that the systems have no IDs; most captured bursts were idle or control/data traffic, and several voice headers ended with FEC errors.

### Git history

- PR #8 added Phase 4 and was merged after the full test and Ruff workflow passed.
- Issue #9 recorded the polarity, slot, evidence-tier, and clipping corrections required for Phase 4.1.

## 7. Phase 4.1 — evidence-quality corrections

Phase 4.1 changes the decoder contract from a binary “one sync line means confirmed” rule to evidence-quality tiers:

```text
dmr_sync_only
dmr_confirmed_degraded
dmr_confirmed_clean
```

The corrected parser and scorer:

- strips ANSI formatting before parsing;
- counts only signed `Sync: +DMR` or `Sync: -DMR` lines as decoder sync evidence;
- does not count generic `Sync: DMR | ... ERR` lines as successful syncs;
- counts only the bracketed active slot, such as `[SLOT1]` or `[slot2]`;
- measures numeric Color Code ratio and dominant-CC consistency;
- records CRC, FEC, CACH, frame-sync, and voice error metrics;
- rewards coherent IDLE/CSBK/DATA activity and complete voice-stage diversity;
- penalizes repetitive single-stage `VC1` patterns typical of false inverted decoding;
- chooses polarity by a documented quality score rather than raw sync count.

Regression testing against all 15 archived real attempts selected `normal` polarity in every case and reproduced the stable Color Code inventory above.

PCM normalization now calculates both a percentile-derived scale and a hard peak-safe scale. The smaller scale is used, so default output contains zero clipped PCM16 samples. Reports preserve the number of samples that would have clipped without the peak cap and whether the limiter was applied.

## 8. Reproducible workflow

```bash
cd ~/Projects/dmr-iq-surveyor
source .venv/bin/activate

./scripts/run_shahar_recordings.sh
./scripts/run_shahar_spectrum.sh
./scripts/run_shahar_detection.sh
./scripts/run_shahar_decode.sh
```

Primary output root:

```text
runs/20260713_163671500Hz/
```

The generated `runs/` directory and source RF recordings remain outside Git.

## 9. Current limitations

- IQ versus QI frequency orientation is still an assumption and should be checked against a known off-center carrier.
- PSD values are relative dBFS/Hz, not calibrated dBm.
- The short recordings are adequate for discovering and confirming DMR carriers, but are not long enough to inventory Talkgroups and Radio IDs reliably.
- Candidates near the recording edges should be re-recorded nearer the receiver center frequency.
- A strong RF signal can still decode poorly because of distortion, frequency offset, transient capture timing, or receiver overload.

## 10. Recommended next work

1. rerun Phase 4 with the Phase 4.1 quality fixes;
2. verify zero clipped samples in every extraction report;
3. confirm that `normal` is selected for all archived attempts;
4. collect longer recordings during active voice traffic;
5. add a session-level inventory that aggregates frequencies, Color Codes, slots, Talkgroups, Radio IDs, and first/last-seen times;
6. independently verify the IQ/QI orientation against a known frequency.
