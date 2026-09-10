"""Project and Campaign manifests, and the rules for combining them with flags.

A manifest is the operator's declaration of what a project is: its id, the
analyzer that reads it, the database that holds it, and the defaults its
campaigns inherit. Nothing here opens a database or touches one -- that is
`project/claim.py`'s job, and keeping the two apart is what lets a manifest be
rendered and checked before anything is written.

Two rules shape the parsing, both borrowed from `survey/profiles.py`, which has
worked this way since Phase 6A:

  * an unknown key is an error, never a silent no-op -- a misspelled default is
    a default that would not apply, and failing is the only way to say so;
  * an id is validated, never repaired. `normalise_project_id` is the sibling of
    `normalise_campaign_id` (`survey/provenance.py`) and shares its rule
    exactly, so one slug means one thing across the whole project.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

MANIFEST_SCHEMA_VERSION = 1

# The one analyzer that exists. VOR/ATIS/DMR are named in the roadmap and are
# not written; a manifest asking for one is refused rather than silently
# treated as P25, because a project pointed at the wrong reader is a project
# whose results mean something other than they say.
ANALYZER_P25_SITE_GEOLOCATION = "p25_site_geolocation"
SUPPORTED_ANALYZERS = (ANALYZER_P25_SITE_GEOLOCATION,)

PROJECT_MANIFEST_NAME = "project.yaml"
CAMPAIGN_DIR_NAME = "campaigns"
_DEFAULT_PROJECT_DIRS = ("projects",)

# Settings where an explicit flag contradicting the manifest is refused rather
# than obeyed. A campaign compares levels between places, so a band silently
# swapped underneath one does not produce a slightly different campaign -- it
# produces measurements that cannot be compared with the ones already in the
# database. Everything else (output directories, recording paths) changes where
# bytes land, not what they mean, so an explicit flag simply wins.
#
# The hardware profile joins this set in PR3, for the same reason.
REFUSE_ON_CONFLICT = frozenset({"band"})

ORIGIN_FLAG = "flag"
ORIGIN_CAMPAIGN = "campaign"
ORIGIN_PROJECT = "project"
ORIGIN_DEFAULT = "default"

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


class ProjectError(ValueError):
    """Raised for an invalid manifest, id, or an irreconcilable conflict."""


def normalise_project_id(value: str | None) -> str | None:
    """The single validator for a project id.

    The same rule as `normalise_campaign_id`, deliberately: an operator should
    not have to remember two slug dialects. Trimming and lowercasing are
    idempotent; anything still not a slug afterwards is rejected rather than
    mangled, so the id typed and the id stored are never two different strings.
    """
    if value is None:
        return None
    candidate = str(value).strip().lower()
    if not candidate:
        return None
    if not _ID_RE.match(candidate):
        raise ProjectError(
            f"invalid project id {value!r}: use 1-64 characters from a-z, 0-9, '.', '_' or '-', "
            "starting with a letter or a digit"
        )
    return candidate


def _load_mapping(path: Path) -> dict[str, Any]:
    """Read a YAML mapping, or say precisely why it is not one."""
    if not path.is_file():
        raise FileNotFoundError(path)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ProjectError(f"{path} is not valid YAML: {exc}") from exc
    if raw is None:
        raise ProjectError(f"{path} is empty")
    if not isinstance(raw, dict):
        raise ProjectError(f"{path} must contain a mapping, found {type(raw).__name__}")
    return raw


def _reject_unknown_keys(raw: dict[str, Any], allowed: set[str], where: str) -> None:
    unknown = set(raw) - allowed
    if unknown:
        raise ProjectError(f"Unknown keys in {where}: {sorted(unknown)}")


def _require(raw: dict[str, Any], keys: tuple[str, ...], where: str) -> None:
    missing = [key for key in keys if raw.get(key) in (None, "")]
    if missing:
        raise ProjectError(f"{where} is missing required keys: {missing}")


def _check_schema_version(raw: dict[str, Any], where: str) -> int:
    version = raw.get("schema_version")
    if version != MANIFEST_SCHEMA_VERSION:
        raise ProjectError(
            f"{where} declares schema_version {version!r}; this build understands "
            f"{MANIFEST_SCHEMA_VERSION}"
        )
    return int(version)


@dataclass(frozen=True, slots=True)
class ProjectDefaults:
    """What a project hands to every campaign that does not override it."""

    band: str | None = None
    site: str | None = None
    output: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"band": self.band, "site": self.site, "output": self.output}


@dataclass(frozen=True, slots=True)
class CampaignDefaults:
    """What one collection round fixes so its stops stay comparable."""

    band: str | None = None
    site: str | None = None
    capture: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"band": self.band, "site": self.site, "capture": dict(self.capture)}


@dataclass(frozen=True, slots=True)
class ProjectManifest:
    schema_version: int
    project_id: str
    label: str
    analyzer: str
    database: Path
    defaults: ProjectDefaults
    path: Path

    @property
    def root(self) -> Path:
        """The directory the manifest lives in; campaigns hang off it."""
        return self.path.parent

    @property
    def campaign_dir(self) -> Path:
        return self.root / CAMPAIGN_DIR_NAME

    def campaign_path(self, campaign_id: str) -> Path:
        return self.campaign_dir / f"{campaign_id}.yaml"


@dataclass(frozen=True, slots=True)
class CampaignManifest:
    schema_version: int
    campaign_id: str
    project_id: str
    label: str
    defaults: CampaignDefaults
    path: Path


_PROJECT_KEYS = {"schema_version", "project_id", "label", "analyzer", "database", "defaults"}
_PROJECT_DEFAULT_KEYS = {"band", "site", "output"}
_CAMPAIGN_KEYS = {"schema_version", "campaign_id", "project_id", "label", "defaults"}
_CAMPAIGN_DEFAULT_KEYS = {"band", "site", "capture"}


def load_project_manifest(path: str | Path) -> ProjectManifest:
    """Parse and fully validate one `project.yaml`. Opens no database."""
    resolved = Path(path).expanduser().resolve()
    return project_from_mapping(_load_mapping(resolved), resolved)


def project_from_mapping(raw: dict[str, Any], resolved: Path) -> ProjectManifest:
    """Validate an already-parsed project mapping.

    Split out so a manifest can be checked while it is still a string in
    memory. Adoption renders one, validates it here, and only then goes
    near a database -- an invalid manifest must never be the reason a
    database was touched.
    """
    _reject_unknown_keys(raw, _PROJECT_KEYS, str(resolved))
    version = _check_schema_version(raw, str(resolved))
    _require(raw, ("project_id", "label", "analyzer", "database"), str(resolved))

    project_id = normalise_project_id(raw["project_id"])
    if project_id is None:
        raise ProjectError(f"{resolved} has an empty project_id")

    analyzer = str(raw["analyzer"])
    if analyzer not in SUPPORTED_ANALYZERS:
        raise ProjectError(
            f"{resolved} asks for analyzer {analyzer!r}, which is not supported. "
            f"This build implements {list(SUPPORTED_ANALYZERS)}"
        )

    defaults_raw = raw.get("defaults") or {}
    if not isinstance(defaults_raw, dict):
        raise ProjectError(f"{resolved}: defaults must be a mapping")
    _reject_unknown_keys(defaults_raw, _PROJECT_DEFAULT_KEYS, f"{resolved} defaults")

    # A relative database path is relative to the manifest, not to whatever
    # directory the operator happened to run the command from. A project that
    # moves keeps working; a project read from elsewhere reads the same file.
    database = Path(str(raw["database"])).expanduser()
    if not database.is_absolute():
        database = resolved.parent / database

    return ProjectManifest(
        schema_version=version,
        project_id=project_id,
        label=str(raw["label"]),
        analyzer=analyzer,
        database=database.resolve(),
        defaults=ProjectDefaults(
            band=_optional_str(defaults_raw.get("band")),
            site=_optional_str(defaults_raw.get("site")),
            output=_optional_str(defaults_raw.get("output")),
        ),
        path=resolved,
    )


RENDER_HEADER = (
    "# Written by `dmr-surveyor project`. Edit by hand freely; every key is\n"
    "# validated on load, and an unknown one is an error rather than ignored.\n"
)


def render_project_manifest(
    *,
    project_id: str,
    label: str,
    analyzer: str,
    database: str | Path,
    defaults: ProjectDefaults | None = None,
) -> str:
    """The text of a project manifest, ready to validate and write."""
    body: dict[str, Any] = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "project_id": project_id,
        "label": label,
        "analyzer": analyzer,
        "database": str(database),
    }
    declared = {
        key: value
        for key, value in (defaults or ProjectDefaults()).to_dict().items()
        if value is not None
    }
    if declared:
        body["defaults"] = declared
    return RENDER_HEADER + yaml.safe_dump(body, sort_keys=False, allow_unicode=True)


def render_campaign_manifest(
    *,
    campaign_id: str,
    project_id: str,
    label: str,
    defaults: CampaignDefaults | None = None,
) -> str:
    """The text of a campaign manifest, ready to validate and write."""
    body: dict[str, Any] = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "campaign_id": campaign_id,
        "project_id": project_id,
        "label": label,
    }
    resolved = defaults or CampaignDefaults()
    declared = {
        key: value
        for key, value in resolved.to_dict().items()
        if value not in (None, {})
    }
    if declared:
        body["defaults"] = declared
    return RENDER_HEADER + yaml.safe_dump(body, sort_keys=False, allow_unicode=True)


def validate_project_text(text: str, source: str | Path) -> ProjectManifest:
    """Check a manifest that has not been written anywhere yet."""
    raw = yaml.safe_load(text)
    if not isinstance(raw, dict):
        raise ProjectError(f"{source} must contain a mapping")
    return project_from_mapping(raw, Path(source).expanduser().resolve())


def load_campaign_manifest(
    path: str | Path, *, expect_project_id: str | None = None, expect_campaign_id: str | None = None
) -> CampaignManifest:
    """Parse and fully validate one campaign manifest.

    The two `expect_*` arguments are what stop a campaign file from being read
    under a project it does not belong to: a file copied between projects, or
    renamed without its contents following, is refused rather than applied.
    """
    resolved = Path(path).expanduser().resolve()
    raw = _load_mapping(resolved)
    _reject_unknown_keys(raw, _CAMPAIGN_KEYS, str(resolved))
    version = _check_schema_version(raw, str(resolved))
    _require(raw, ("campaign_id", "project_id", "label"), str(resolved))

    # The campaign id uses the campaign validator, not a second dialect.
    from dmr_iq_surveyor.survey.provenance import ProvenanceError, normalise_campaign_id

    try:
        campaign_id = normalise_campaign_id(raw["campaign_id"])
    except ProvenanceError as exc:
        raise ProjectError(f"{resolved}: {exc}") from exc
    if campaign_id is None:
        raise ProjectError(f"{resolved} has an empty campaign_id")

    project_id = normalise_project_id(raw["project_id"])
    if project_id is None:
        raise ProjectError(f"{resolved} has an empty project_id")

    stem = normalise_project_id(resolved.stem) if _ID_RE.match(resolved.stem.lower()) else None
    if stem is not None and stem != campaign_id:
        raise ProjectError(
            f"{resolved} declares campaign_id {campaign_id!r} but its filename says {stem!r}; "
            "the two must agree so a campaign can be found by name"
        )
    if expect_campaign_id is not None and campaign_id != expect_campaign_id:
        raise ProjectError(
            f"{resolved} declares campaign_id {campaign_id!r}, but {expect_campaign_id!r} was asked for"
        )
    if expect_project_id is not None and project_id != expect_project_id:
        raise ProjectError(
            f"{resolved} belongs to project {project_id!r}, not {expect_project_id!r}"
        )

    defaults_raw = raw.get("defaults") or {}
    if not isinstance(defaults_raw, dict):
        raise ProjectError(f"{resolved}: defaults must be a mapping")
    _reject_unknown_keys(defaults_raw, _CAMPAIGN_DEFAULT_KEYS, f"{resolved} defaults")
    capture = defaults_raw.get("capture") or {}
    if not isinstance(capture, dict):
        raise ProjectError(f"{resolved}: defaults.capture must be a mapping")

    return CampaignManifest(
        schema_version=version,
        campaign_id=campaign_id,
        project_id=project_id,
        label=str(raw["label"]),
        defaults=CampaignDefaults(
            band=_optional_str(defaults_raw.get("band")),
            site=_optional_str(defaults_raw.get("site")),
            capture=dict(capture),
        ),
        path=resolved,
    )


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def resolve_project(
    project: str | Path,
    *,
    search_dirs: tuple[str, ...] = _DEFAULT_PROJECT_DIRS,
    base_dir: str | Path = ".",
) -> ProjectManifest:
    """A path to a manifest, or a name looked up under `projects/<name>/`.

    The same two-step shape as `resolve_band_profile`, so an operator who knows
    how band profiles resolve already knows how this does.
    """
    candidate = Path(project).expanduser()
    if candidate.is_file():
        return load_project_manifest(candidate)
    if candidate.is_dir() and (candidate / PROJECT_MANIFEST_NAME).is_file():
        return load_project_manifest(candidate / PROJECT_MANIFEST_NAME)

    base = Path(base_dir).expanduser().resolve()
    tried = [str(candidate)]
    for directory in search_dirs:
        guess = base / directory / str(project) / PROJECT_MANIFEST_NAME
        tried.append(str(guess))
        if guess.is_file():
            return load_project_manifest(guess)
    raise ProjectError(f"Project manifest not found. Tried: {tried}")


def resolve_campaign(project: ProjectManifest, campaign_id: str) -> CampaignManifest:
    """The campaign file for this project, which must already exist.

    A campaign is never invented. Asking for one that has no manifest is an
    error, because the alternative is a run tagged with a collection round
    nobody declared and whose settings nothing pins.
    """
    from dmr_iq_surveyor.survey.provenance import ProvenanceError, normalise_campaign_id

    try:
        wanted = normalise_campaign_id(campaign_id)
    except ProvenanceError as exc:
        raise ProjectError(str(exc)) from exc
    if wanted is None:
        raise ProjectError("a campaign id is required to resolve a campaign manifest")

    path = project.campaign_path(wanted)
    if not path.is_file():
        raise ProjectError(
            f"no campaign manifest at {path}. Create it with "
            f"`dmr-surveyor project campaign new --project {project.project_id} "
            f"--campaign-id {wanted}`"
        )
    return load_campaign_manifest(
        path, expect_project_id=project.project_id, expect_campaign_id=wanted
    )


@dataclass(frozen=True, slots=True)
class Resolution:
    """One setting, and which of the four sources decided it."""

    value: Any
    origin: str

    @property
    def from_manifest(self) -> bool:
        return self.origin in (ORIGIN_CAMPAIGN, ORIGIN_PROJECT)


def resolve_setting(
    key: str,
    *,
    flag: Any,
    flag_explicit: bool,
    campaign: Any = None,
    project: Any = None,
    default: Any = None,
) -> Resolution:
    """Combine one setting from flag, campaign, project and CLI default.

    Precedence is flag, then campaign, then project, then the CLI's own
    default. `flag_explicit` is what makes that honest: several flags have a
    non-None default, so the value alone cannot say whether the operator typed
    it. Callers get that from Click's parameter source.

    For the keys in `REFUSE_ON_CONFLICT` an explicit flag that disagrees with a
    manifest is an error rather than an override, and the message names both
    values and where each came from.
    """
    manifest_value = campaign if campaign is not None else project
    manifest_origin = ORIGIN_CAMPAIGN if campaign is not None else ORIGIN_PROJECT

    if flag_explicit:
        if (
            key in REFUSE_ON_CONFLICT
            and manifest_value is not None
            and manifest_value != flag
        ):
            raise ProjectError(
                f"--{key} was given as {flag!r} but the {manifest_origin} manifest says "
                f"{manifest_value!r}. Levels recorded under different {key} settings are not "
                f"comparable, so this is refused rather than applied. Change the manifest, or "
                f"drop --{key}."
            )
        return Resolution(flag, ORIGIN_FLAG)

    if manifest_value is not None:
        return Resolution(manifest_value, manifest_origin)
    return Resolution(default if default is not None else flag, ORIGIN_DEFAULT)


__all__ = [
    "ANALYZER_P25_SITE_GEOLOCATION",
    "CAMPAIGN_DIR_NAME",
    "MANIFEST_SCHEMA_VERSION",
    "ORIGIN_CAMPAIGN",
    "ORIGIN_DEFAULT",
    "ORIGIN_FLAG",
    "ORIGIN_PROJECT",
    "PROJECT_MANIFEST_NAME",
    "REFUSE_ON_CONFLICT",
    "SUPPORTED_ANALYZERS",
    "CampaignDefaults",
    "CampaignManifest",
    "ProjectDefaults",
    "ProjectError",
    "ProjectManifest",
    "Resolution",
    "load_campaign_manifest",
    "load_project_manifest",
    "normalise_project_id",
    "resolve_campaign",
    "resolve_project",
    "resolve_setting",
]
