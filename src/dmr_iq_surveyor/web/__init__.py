"""Phase 7 field web app: mark a position, record, watch, see the map.

Standard-library HTTP server plus a dependency-free single-page app. See
`docs/phase7-geolocation-design.md`.
"""

from dmr_iq_surveyor.web.jobs import Job, JobRegistry
from dmr_iq_surveyor.web.server import create_server, serve_forever
from dmr_iq_surveyor.web.service import FieldService, FieldSettings

__all__ = [
    "FieldService",
    "FieldSettings",
    "Job",
    "JobRegistry",
    "create_server",
    "serve_forever",
]
