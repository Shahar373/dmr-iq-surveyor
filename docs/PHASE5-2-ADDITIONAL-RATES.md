# Phase 5.2 — additional SDRconnect rates

Phase 5.2 adds exact-rate extraction profiles for field recordings discovered on
17 July 2026. It preserves the existing fail-fast contract: recordings are never
silently processed with a profile designed for another sample rate.

## Profiles

| Profile | Exact input rate | Complex intermediate rate | Intended use |
|---|---:|---:|---|
| `10m` | 10,000,000 S/s | 100,000 S/s | established wideband workflow |
| `5m` | 5,000,000 S/s | 100,000 S/s | SDRconnect 5 MHz wideband captures |
| `500k` | 500,000 S/s | 100,000 S/s | preferred long targeted captures |
| `250k` | 250,000 S/s | 50,000 S/s | compact targeted captures |
| `62k5` | 62,500 S/s | 62,500 S/s | narrow SDRconnect IQ captures |
| `auto` | exact detected match | profile-dependent | exact-rate selection |

The `5m` path mixes the selected channel to baseband, decimates 10:1 and then
5:1, filters the channel at 7.5 kHz, FM-discriminates and resamples to 48 kHz.

The `62k5` path mixes the selected channel to baseband without decimation,
filters at the native rate, FM-discriminates and resamples to 48 kHz. This is
suitable for the real SDRconnect recording centered at 164.556000 MHz with the
164.537500 MHz channel 18.5 kHz below center.

## Real recordings

```text
/home/shahar/Documents/SDRconnect_IQ_20260717_160721_164556000HZ.wav
  rate:   62,500 S/s
  target: 164.537500 MHz

/home/shahar/Documents/SDRconnect_IQ_20260717_162218_164556000HZ.wav
  rate:   5,000,000 S/s
  center: 164.556000 MHz
```

The 5 MHz recording covers approximately 162.056000–167.056000 MHz. It does not
contain the previously confirmed 167.137500 MHz channel.

## Targeted 62.5 kS/s decode

```bash
dmr-surveyor targeted-decode \
  /home/shahar/Documents/SDRconnect_IQ_20260717_160721_164556000HZ.wav \
  --frequency 164537500 \
  --profile auto \
  --metadata config/targeted_164537500_sdrconnect_160721.yaml \
  --run-id targeted_20260717_sdrconnect_160721 \
  --output runs/targeted/targeted_20260717_sdrconnect_160721 \
  --database runs/inventory/dmr_inventory.sqlite3
```

The metadata must declare the recording geometry, not only the target channel:

```yaml
center_frequency_hz: 164556000
sample_rate_hz: 62500
target_frequency_hz: 164537500
recording_software: SDRconnect
```

## Wideband 5 MS/s candidate decode

Use a separate run ID for every frequency so every result accumulates in the
persistent inventory. The initial list contains only candidates classified as
`dmr_like_narrowband` by Phase 3:

```bash
WIDE=/home/shahar/Documents/SDRconnect_IQ_20260717_162218_164556000HZ.wav
META=config/wide5m_sdrconnect_20260717_162218.yaml

for FREQ in \
  162137500 162262500 162475000 162525000 162587500 162675000 \
  163068750 163637500 164106250 164300000 164325000 164537500 \
  164637500 164725000 165600000 165625000 166662500 166681250
do
  RUN="wide5m_20260717_${FREQ}"
  dmr-surveyor targeted-decode "$WIDE" \
    --frequency "$FREQ" \
    --profile 5m \
    --metadata "$META" \
    --run-id "$RUN" \
    --output "runs/targeted/$RUN" \
    --database runs/inventory/dmr_inventory.sqlite3 || break
done
```

The wideband metadata must include:

```yaml
source_started_at: "2026-07-17T16:22:18+03:00"
center_frequency_hz: 164556000
sample_rate_hz: 5000000
recording_software: SDRconnect
sdr_model: SDRplay RSP1B
notes: "5 MHz wideband DMR candidate survey"
```

## Result packaging

Package generated products, reports and the persistent inventory. Do not include
raw IQ files in the archive:

```bash
python3 scripts/package_results.py \
  --output sdrconnect_20260717_decoded_results.zip \
  runs/sdrconnect_20260717_162218_wide5m \
  runs/targeted/targeted_20260717_sdrconnect_160721 \
  runs/targeted/wide5m_20260717_* \
  runs/inventory/dmr_inventory.sqlite3 \
  config/targeted_164537500_sdrconnect_160721.yaml \
  config/wide5m_sdrconnect_20260717_162218.yaml

sha256sum sdrconnect_20260717_decoded_results.zip \
  > sdrconnect_20260717_decoded_results.zip.sha256
```

## Interpretation

A Phase 3 candidate remains a spectral hypothesis until DSD-FME provides signed
DMR sync and coherent Color Code/activity evidence. Explicit Talkgroup and Radio
IDs are retained only when printed by the decoder and parsed by Phase 5.

The workflow is passive and receive-only.
