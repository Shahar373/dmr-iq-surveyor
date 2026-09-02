# Phase 6 design — generic RF survey and P25

Phase 6 extends `dmr-iq-surveyor` from a DMR-only pipeline into a general passive RF survey system, without renaming the repository, breaking existing commands, or discarding DMR functionality. The first new protocol target is P25 in the 866–870 MHz public-safety segment.

This document is the overview across milestones. `docs/phase6a-survey.md` covers Phase 6A (generic survey and comparison, implemented) in full usage detail.

## Principle: discovery before reference

The system looks at real RF first and only afterwards compares it to external reference data (e.g. a RadioReference snapshot). Reference data never influences what the detector looks for. `survey/` has no import from `reference/`; a later milestone's matching step reads both, only after observations are already stored.

## Principle: evidence-first classification

A spectral shape match is a *candidate*, never a confirmation. Phase 6A never runs a protocol decoder, so every observation's `classification` is `unknown` with `classification_method="spectral_only"`. `spectral_class` carries the spectral-shape hypothesis (e.g. `narrowband_digital_candidate`) and must never be read as a protocol confirmation — that requires actual decoder evidence, added in Phase 6B.

## Milestones

| Milestone | Objective | Status |
|---|---|---|
| 6.0 | Green, reproducible CI (pinned ruff, honest `CLAUDE.md`) | done |
| 6A | Generic RF survey: discovery, persistent inventory, protocol-agnostic run comparison | done |
| 6B | P25 evidence-based classification via DSD-FME, backend-neutral channel IQ retained | planned |
| 6C | P25 control-channel role, system/site metadata, channel grants | planned |
| 6D | Reference snapshot import/matching | done, as Phase 7's `reference/` registry |
| 6E | Explainable round-2 follow-up recommendations (no retuning) | planned |
| 6F | Dashboard | done, as Phase 7's served field app rather than a standalone HTML file |
| 6G | Live SoapySDR acquisition feeding the same pipeline | done, `capture/` + `survey capture` |
| 7 | Multi-session P25 site geolocation and the field web app | done, see `docs/phase7-geolocation-design.md` |

6B remains the most consequential gap. Until a control channel is actually decoded, a site is
attributed to a measurement by frequency alone, which the 6D registry can only report honestly
(`inferred_unique`), never confirm — and cannot do at all where two sites share a frequency.

## Architecture

```
src/dmr_iq_surveyor/
    iq/, spectrum/, detect/, decode/    unchanged core, reused as-is
    survey/                             Phase 6A: profiles, discovery, store, compare, pipeline
    reporting/                          JSON/Markdown report rendering
    protocols/                          Phase 6B+: P25/DMR probe adapters (not yet added)
    reference/                          external P25 site snapshots (Phase 7)
    geo/                                measurements, propagation model, grid posterior (Phase 7)
    web/                                field control app (Phase 7)
    inventory/                          unchanged (DMR-specific persistent inventory)
    cli.py -> cli_v2 -> cli_v3 -> cli_v4  unchanged additive command chain
    cli_survey.py, cli_app.py           new `survey` sub-app, new console entry point
```

`cli_app.py` is the new console entry point (`dmr-surveyor = dmr_iq_surveyor.cli_app:app`). It imports `cli_v4` (which imports the whole `cli` → `cli_v2` → `cli_v3` → `cli_v4` chain) and mounts `survey_app`, `geo_app` and `web_app` as Typer sub-apps. `cli_v4:app` still works unchanged for anyone importing it directly; nothing was deleted or renamed.

## Database

One SQLite database, extended additively. `survey.store.connect_survey_database()` calls the existing `inventory.store.connect_database()` first (which owns `runs`/`attempts`/`events`/`sessions`/`channels`, unchanged), then applies the new survey schema. An existing Pi database upgrades in place with no data loss and no user action; this is tested against a database built entirely by the pre-Phase-6 code (`tests/test_survey_store.py::test_existing_dmr_database_opens_and_extends_cleanly`).

New tables: `sites`, `survey_runs`, `rf_frequencies`, `rf_observations`, `run_comparisons`. See `docs/phase6a-survey.md` for the schema and the rules that keep it idempotent and protocol-neutral.

## Backward compatibility

- No existing command's name, arguments, defaults, or output paths changed.
- No existing file was deleted or moved.
- `detect.core.classify_features()` still returns `dmr_like_narrowband` and friends unchanged; `spectral_class` is an *additional* field, so existing `config/*.yaml` `phase4.candidate_classes` selectors keep working.
- `spectrum/core.py` and `spectrum/runner.py` are untouched by Phase 6A. Segmented, time-windowed analysis for surveys is implemented as an independent code path in `survey/discovery.py` that reuses the same FFT/PSD primitives, rather than adding parameters to `SpectrumSettings` — this makes the byte-identical behavior of `run_spectrum()` a structural guarantee, not something that has to be separately verified after every future change. `tests/test_survey_pipeline.py::test_spectrum_settings_has_no_segment_fields` guards against that changing by accident.
- All 50 pre-Phase-6 tests pass unmodified.
