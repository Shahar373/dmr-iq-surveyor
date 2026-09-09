"""Console entry point: pulls in the full command chain (Phases 1-5.2 plus
the Phase 6 survey and Phase 7 geolocation commands) and mounts the sub-apps.

`cli_v4:app` continues to work unchanged for anyone importing it directly
(scripts, tests) -- this module only adds to it, matching how cli_v2/v3/v4
each additively registered commands on the same underlying Typer app.
"""

from __future__ import annotations

from dmr_iq_surveyor.cli_geo import geo_app
from dmr_iq_surveyor.cli_live import live_app
from dmr_iq_surveyor.cli_survey import survey_app
from dmr_iq_surveyor.cli_v4 import app, console
from dmr_iq_surveyor.cli_web import web_app

app.add_typer(survey_app, name="survey")
app.add_typer(geo_app, name="geo")
app.add_typer(web_app, name="web")
app.add_typer(live_app, name="live")

__all__ = ["app", "console"]

if __name__ == "__main__":
    app()
