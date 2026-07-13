# Phase 1 — SDRconnect IQ inspection

This phase inspects RIFF/RF64 SDRconnect recordings, reads metadata without loading the full file into RAM, samples bounded IQ windows, and writes diagnostic reports.

The batch runner is configured for Shahar's two recordings under `config/shahar_recordings.yaml`.

The center-frequency logic prefers `auxi` metadata and falls back to SDRconnect-style filename suffixes such as `_163671500HZ`. Missing values remain explicit and no longer crash report generation.
