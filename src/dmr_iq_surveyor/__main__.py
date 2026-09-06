"""`python -m dmr_iq_surveyor`.

Imports the same app as the `dmr-surveyor` console script so both entry
points expose the same commands. It previously imported `cli_v4`, which
predates the `survey`, `geo` and `web` sub-apps and so offered fewer
commands than the installed script; nothing is removed by this.
"""

from dmr_iq_surveyor.cli_app import app

if __name__ == "__main__":
    app()
