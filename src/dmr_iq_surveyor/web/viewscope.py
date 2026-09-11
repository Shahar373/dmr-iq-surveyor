"""What the operator is looking at, which is not what the app records into.

PR3 gave the field app a campaign and then used that one value twice: it was
the id stamped onto every new stop, *and* it was the filter on every read the
page made. Setting `FIELD_CAMPAIGN` therefore did two things at once, and only
one of them was asked for -- the second silently emptied the map, the stop
list and the site summary of 51 rounds of earlier work. Nothing was deleted;
`campaign_id = 'g4'` is simply never true for a row holding `NULL`.

So the two concepts are separated here:

* **capture campaign** -- `FieldSettings.capture_campaign_id`. The one and
  only place new evidence is written. A view never changes it.
* **view scope** -- this module. Per request, defaulting to `current`, and
  never remembered anywhere. A reload is back on the campaign being recorded,
  which is the state the operator must never be surprised out of.

Four views:

``current``   the capture campaign -- the default, and the only writable one.
``legacy``    `campaign_id IS NULL`: the rounds recorded before campaigns.
``all``       every round in the file, read-only and grouped by campaign.
``campaign:<id>``  one other named round, read-only.

`legacy` is spelled out rather than expressed as an absent value on purpose.
`None` was already spoken for -- in `CampaignScope` it means the whole
database -- so a third concept needed a third name rather than a second
meaning for a value that already had one.

Everything but `current` is read-only. Not because browsing is dangerous, but
because the alternative is an operator who excludes a stop, or taps Record,
while looking at a screen full of last month's work and believing it is this
morning's.
"""

from __future__ import annotations

from dataclasses import dataclass

from dmr_iq_surveyor.survey.provenance import ProvenanceError, normalise_campaign_id
from dmr_iq_surveyor.survey.scope import UNASSIGNED_ONLY, WHOLE_DATABASE, CampaignScope

CURRENT = "current"
LEGACY = "legacy"
ALL = "all"
CAMPAIGN = "campaign"
CAMPAIGN_PREFIX = f"{CAMPAIGN}:"


class ViewScopeError(ValueError):
    """Raised for a view scope that cannot be read as one of the four above."""


class ReadOnlyViewError(RuntimeError):
    """Raised when a write is attempted while the request names a read-only view.

    A `RuntimeError`, so the HTTP layer answers 409 rather than 400: the
    request is well formed and the operator is allowed to make it -- just not
    from where they are standing. Switching back to the current campaign is
    the fix, and the message says so.
    """


@dataclass(frozen=True, slots=True)
class ViewScope:
    """One browsing choice. Never persisted, never a write target."""

    kind: str = CURRENT
    campaign_id: str | None = None

    @property
    def token(self) -> str:
        """The value that round-trips through `?scope=` and `/api/state`."""
        if self.kind == CAMPAIGN:
            return f"{CAMPAIGN_PREFIX}{self.campaign_id}"
        return self.kind

    @property
    def is_read_only(self) -> bool:
        return self.kind != CURRENT

    @property
    def groups_by_campaign(self) -> bool:
        """Whether this view spans rounds and so must show them apart.

        Only `all` does. Every other view holds exactly one boundary, so
        there is nothing to group and the reads stay exactly as they were.
        """
        return self.kind == ALL

    def label(self, capture_campaign_id: str | None) -> str:
        """One phrase an operator can read off a status bar."""
        if self.kind == LEGACY:
            return "legacy (unassigned)"
        if self.kind == ALL:
            return "all campaigns (overview)"
        if self.kind == CAMPAIGN:
            return f"campaign {self.campaign_id}"
        if capture_campaign_id:
            return f"current campaign ({capture_campaign_id})"
        return "current (whole database -- no campaign set)"

    def read_scope(self, capture_campaign_id: str | None) -> CampaignScope:
        """The analysis boundary this view reads through.

        `current` is exactly what the app did before this module existed,
        including the unconfigured deployment whose capture campaign is
        `None` and whose reads have always covered the whole file.
        """
        if self.kind == LEGACY:
            return UNASSIGNED_ONLY
        if self.kind == ALL:
            return WHOLE_DATABASE
        if self.kind == CAMPAIGN:
            return CampaignScope(self.campaign_id)
        return CampaignScope(capture_campaign_id)


CURRENT_VIEW = ViewScope(CURRENT)
LEGACY_VIEW = ViewScope(LEGACY)
ALL_VIEW = ViewScope(ALL)


def parse_view_scope(raw: str | None, *, capture_campaign_id: str | None = None) -> ViewScope:
    """Read a `?scope=` value, or refuse it in words the operator can act on.

    An absent or empty value is `current`. That is what makes every client
    that predates this parameter -- and every hand-typed URL -- behave exactly
    as it did, and it is what makes a reload land back on the campaign being
    recorded.
    """
    text = (raw or "").strip().lower()
    if not text or text == CURRENT:
        return CURRENT_VIEW
    if text == LEGACY:
        return LEGACY_VIEW
    if text == ALL:
        return ALL_VIEW
    if text.startswith(CAMPAIGN_PREFIX):
        try:
            campaign_id = normalise_campaign_id(text[len(CAMPAIGN_PREFIX) :])
        except ProvenanceError as exc:
            raise ViewScopeError(f"unusable campaign in view scope {raw!r}: {exc}") from exc
        if campaign_id is None:
            raise ViewScopeError(
                f"view scope {raw!r} names no campaign. Use 'legacy' for the runs "
                "that carry no campaign, or 'all' for every round in the file"
            )
        # A view onto the campaign being recorded is the current view, not a
        # read-only copy of it. Naming your own round explicitly must not
        # take Record away from you.
        if campaign_id == capture_campaign_id:
            return CURRENT_VIEW
        return ViewScope(CAMPAIGN, campaign_id)
    raise ViewScopeError(
        f"unknown view scope {raw!r}. Use 'current' (the campaign being recorded), "
        "'legacy' (rounds recorded before campaigns), 'all' (every round, read-only) "
        "or 'campaign:<id>'"
    )


def refuse_if_read_only(view: ViewScope, action: str, capture_campaign_id: str | None) -> None:
    """Stop a write that was asked for from a historical view."""
    if not view.is_read_only:
        return
    target = capture_campaign_id or "the whole database"
    raise ReadOnlyViewError(
        f"{action} is refused while the view is {view.label(capture_campaign_id)}: "
        f"it is read-only. New work is always recorded into {target}, so switch "
        "back to the current campaign first"
    )


__all__ = [
    "ALL_VIEW",
    "CURRENT_VIEW",
    "LEGACY_VIEW",
    "ReadOnlyViewError",
    "ViewScope",
    "ViewScopeError",
    "parse_view_scope",
    "refuse_if_read_only",
]
