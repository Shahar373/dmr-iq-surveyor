"""Phase 0.5 stabilization (Q7): the interim IQ-retention policy.

No event-triggered IQ snippet mechanism exists yet, so keeping zero
recordings would leave nothing to replay a field anomaly against. Until
snippets are built, the field app keeps the most recent recording by
default. These tests pin the new default in both places it is set;
nothing about the retention/ledger mechanism itself changes here.
"""

from __future__ import annotations

import inspect

from dmr_iq_surveyor.cli_web import web_serve
from dmr_iq_surveyor.web.service import FieldSettings


def test_field_settings_keeps_one_recording_by_default() -> None:
    assert FieldSettings().keep_recordings == 1


def test_web_serve_cli_keeps_one_recording_by_default() -> None:
    default = inspect.signature(web_serve).parameters["keep_recordings"].default
    assert default == 1
