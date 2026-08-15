"""Phase 6 generic RF survey: discovery, persistent inventory, comparison.

Protocol-agnostic. No P25/DMR decoding happens here -- that is Phase 6B
(`dmr_iq_surveyor.protocols`). Reference-data comparison is a later
milestone; this package never imports it, keeping discovery uncontaminated
by expectations from external data.
"""

from dmr_iq_surveyor.survey.compare import ComparisonRow, compare_runs
from dmr_iq_surveyor.survey.discovery import RfObservation, discover_observations
from dmr_iq_surveyor.survey.pipeline import run_comparison, run_survey
from dmr_iq_surveyor.survey.profiles import (
    BandProfile,
    SiteProfile,
    resolve_band_profile,
    resolve_site_profile,
)

__all__ = [
    "BandProfile",
    "ComparisonRow",
    "RfObservation",
    "SiteProfile",
    "compare_runs",
    "discover_observations",
    "resolve_band_profile",
    "resolve_site_profile",
    "run_comparison",
    "run_survey",
]
