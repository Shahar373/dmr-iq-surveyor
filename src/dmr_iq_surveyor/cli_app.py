"""Console entry point: pulls in the full command chain (Phases 1-5.2 plus
Phase 6 survey commands) and mounts the Phase 6 sub-apps.

`cli_v4:app` continues to work unchanged for anyone importing it directly
(scripts, tests) -- this module only adds to it, matching how cli_v2/v3/v4
each additively registered commands on the same underlying Typer app.
"""

from __future__ import annotations

from dmr_iq_surveyor.cli_survey import survey_app
from dmr_iq_surveyor.cli_v4 import app, console

app.add_typer(survey_app, name="survey")

__all__ = ["app", "console"]

if __name__ == "__main__":
    app()
