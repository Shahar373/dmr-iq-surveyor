# Known limitations

- The project currently performs container inspection only; it does not yet detect or decode DMR channels.
- IQ order is assumed from SDRconnect convention and is not proven statistically.
- 24-bit packed PCM is not supported.
- Filename-derived center frequency is a fallback and is recorded as such.
- Generated `runs/` output and source IQ recordings are intentionally excluded from Git.
