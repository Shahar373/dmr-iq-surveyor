# Instructions for Claude Code

Read these files first:

1. `README.md`
2. `src/dmr_iq_surveyor/iq/metadata.py`
3. `tests/test_metadata.py`

The repository currently implements Milestone 1 only.

Before changing code:

- run `pytest`
- inspect real run artifacts under `runs/`
- do not infer DMR from bandwidth alone
- do not implement UI yet
- do not load the full wideband recording into RAM
- do not overwrite source recordings
