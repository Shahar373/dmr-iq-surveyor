# Instructions for Claude Code

Read these files first:

1. `README.md`
2. `src/dmr_iq_surveyor/iq/metadata.py`
3. `tests/test_metadata.py`

If a plan document exists under `docs/phase6-design.md`, `docs/phase6a-survey.md` or
`docs/phase7-geolocation-design.md`, read it too — those are the authoritative designs for the RF
survey / P25 / geolocation work.

## Current state

This is not a Milestone-1 prototype. The repository implements Phases 1–5.2 of an offline, passive,
receive-only DMR survey pipeline (inspect → spectrum → detect → extract/decode → SQLite inventory →
targeted capture), Phase 6A (generic multi-protocol RF survey), live SoapySDR capture (`capture/`),
and Phase 7 (multi-session P25 site geolocation plus a served field app: `reference/`, `geo/`,
`web/`). Do not assume any part of the pipeline is unfinished or a stub — check the actual code and
tests before describing what exists.

What is genuinely *not* implemented is protocol decoding for P25 (Phase 6B). Site attribution is
therefore by frequency alone (`inferred_unique`), and a frequency shared by two sites is excluded
(`ambiguous_reuse`) rather than guessed. Do not describe a geolocation result as confirming which
site was heard.

`cli.py`, `cli_v2.py`, `cli_v3.py`, `cli_v4.py` are not competing versions. Each imports the previous
module's `app` and adds commands for one phase; the console entry point pulls in the whole chain
through imports. This is intentional additive structure, not dead code — do not delete or "clean up"
these files without a specific reason tied to a phase's plan.

## Architecture principles

- **Discovery before reference.** The system must look at real RF first and only afterwards compare
  it to external reference data (e.g. RadioReference snapshots). Reference data must never influence
  what the detector looks for or expects to see.
- **Evidence-first classification.** Never report a protocol as confirmed from spectral shape alone.
  A spectral match is a *candidate*, not a confirmation. DMR and P25 confirmation both require actual
  decoder evidence (sync, valid frames, consistent identifiers). Do not infer a protocol from
  bandwidth alone.
- **Missing is not null.** When information isn't available, say why:
  `unknown | not_observed | not_supported | decoder_failed`, not a bare `None` everywhere the reason
  is actually distinguishable.
- **Passive, receive-only scope.** No transmission, injection, spoofing, jamming, authentication
  bypass, or decryption. Encrypted traffic may be identified as encrypted; it is never decrypted.
- **No premature ML.** Classification is rule/evidence-based and must be explainable. Don't add a
  learned classifier for protocol identification.
- **Offline analysis is still the spine.** Every stage must work file-in, artifacts-and-database-out.
  Live acquisition (`capture/`) and the field app (`web/`) were each explicitly authorized and are
  built strictly as thin compositions over that spine — `web/service.py` calls `run_capture`,
  `run_survey` and `geo.pipeline` unchanged. Do not reimplement pipeline logic inside them, and do
  not start new realtime or UI surfaces opportunistically while working on an earlier phase.

## Memory and performance constraints

- Never load a full wideband IQ recording into RAM. Use the existing memmap (`iq/reader.py`) and
  chunked/streamed processing patterns (see `spectrum/runner.py`, `decode/core.py`). This matters
  because the target deployment is a Raspberry Pi and captures can be tens of gigabytes.
- Heavy stages must be bounded and, where a full-file pass isn't necessary, prefer segmented/strided
  analysis over one unbounded pass.
- Don't reach for aggressive multiprocessing as a default; keep behavior deterministic.

## Backward compatibility (hard requirement)

- Never overwrite a source recording.
- Existing CLI commands, their arguments, defaults, and output paths must keep working exactly as
  they do today. Add new capability as new commands/sub-apps, not by changing existing ones.
- Existing SQLite tables and their semantics must not change. Additive schema changes only
  (`CREATE TABLE IF NOT EXISTS`, new columns with safe defaults); an existing database upgrades in
  place with no manual user action and no data loss.
- Existing spectral/classification labels (e.g. `dmr_like_narrowband`) must keep being emitted
  exactly as before, since batch configs select on them; add new labels alongside, don't rename.
- All existing tests must keep passing unmodified. If a change seems to require editing an existing
  test, that's a signal the design needs to change, not the test.

## Before changing code

- Run `pytest -q` and `ruff check .` and confirm both are clean before you start, so you know
  whether a failure you see later was caused by your change.
- Inspect real run artifacts under `runs/` if they exist locally (this directory is gitignored and
  not part of the repository; it will not exist in a fresh clone or CI).
- Do not infer DMR or P25 from bandwidth/spectral shape alone.
- Do not implement UI ahead of the milestone that calls for it.
- Do not present a geolocation posterior mode as a transmitter coordinate, or drop a site/measurement
  that cannot be used — record the reason (`ambiguous_reuse`, `not_covered`, `frequency_unknown`,
  `insufficient_evidence`, `unbounded_region`, `weak_geometry`) instead.
- Do not load the full wideband recording into RAM.
- Do not overwrite source recordings.
