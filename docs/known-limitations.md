# Known limitations (historical, Phase 1)

This document describes the project as it stood at Phase 1 (container inspection only), before
detection, decoding, survey, capture or geolocation existed. It is kept as the historical record of
that milestone. **For the current state's known issues, see
[`docs/known-issues-v0.10.md`](known-issues-v0.10.md).**

- The project at Phase 1 performed container inspection only; it did not yet detect or decode DMR
  channels. DMR detection, decoding, RF survey and P25 geolocation were added in later phases (see
  `README.md`).
- IQ order is assumed from SDRconnect convention and is not proven statistically.
- 24-bit packed PCM is not supported.
- Filename-derived center frequency is a fallback and is recorded as such.
- Generated `runs/` output and source IQ recordings are intentionally excluded from Git.
