"""What an adversarial review of PR5 found, and what stops each of it coming back.

Every test here failed against `8db70d1`, the head the review ran on. They are
grouped by the defect they pin rather than by the file they touch, because
each one is a specific way the feature lied: a cleanup that cleaned nothing, a
check that passed when it could not see, a state machine that reported a
switch it had not made, and a serial that no radio ever reported.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

from dmr_iq_surveyor.capture.device import device_identity
from dmr_iq_surveyor.project.claim import write_manifest_atomically
from dmr_iq_surveyor.project.manifest import (
    CAMPAIGN_STATUS_CLOSED,
    ProjectError,
    load_campaign_manifest,
    set_campaign_status_text,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
FIELDCTL = REPO_ROOT / "scripts" / "fieldctl"


# -- the cleanup that cleaned nothing -------------------------------------


def _shell_function(name: str) -> str:
    """One function lifted out of the script, so it can be exercised alone."""
    source = FIELDCTL.read_text(encoding="utf-8").splitlines()
    start = next(i for i, line in enumerate(source) if line.startswith(f"{name}() {{"))
    end = next(i for i in range(start, len(source)) if source[i] == "}")
    return "\n".join(source[start : end + 1])


def test_a_scratch_file_is_registered_in_the_shell_that_will_clean_it_up(
    tmp_path: Path,
) -> None:
    """`x="$(scratch_file)"` ran the registration in a subshell and threw it
    away, so the array was always empty, the EXIT trap was a no-op, and the
    curl config holding the API token outlived every Ctrl-C."""
    script = tmp_path / "probe.sh"
    script.write_text(
        "set -euo pipefail\n"
        "die() { printf '%s\\n' \"$*\" >&2; exit 1; }\n"
        "SCRATCH_FILES=()\n"
        + _shell_function("scratch_file")
        + "\nscratch_file first\nscratch_file second\n"
        'printf "registered=%s\\n" "${#SCRATCH_FILES[@]}"\n'
        'printf "first_exists=%s\\n" "$([[ -f "$first" ]] && echo yes)"\n'
        'rm -f "$first" "$second"\n',
        encoding="utf-8",
    )

    result = subprocess.run(
        ["bash", str(script)], capture_output=True, text=True, timeout=60, check=False
    )

    assert result.returncode == 0, result.stderr
    assert "registered=2" in result.stdout
    assert "first_exists=yes" in result.stdout


def test_the_token_config_does_not_survive_an_interrupted_command(
    tmp_path: Path,
) -> None:
    """The end-to-end version: a signal while curl is the foreground child
    used to leave the shared token readable in $TMPDIR, which is exactly the
    secret-on-disk nobody would think to look for."""
    token_value = "s3cret-token-value"
    binaries = tmp_path / "bin"
    scratch = tmp_path / "tmp"
    binaries.mkdir()
    scratch.mkdir()
    (binaries / "curl").write_text("#!/bin/sh\nsleep 30\n", encoding="utf-8")
    (binaries / "systemctl").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    for stub in binaries.iterdir():
        stub.chmod(0o755)
    token = tmp_path / "token"
    token.write_text(token_value + "\n", encoding="utf-8")
    token.chmod(0o600)
    site = tmp_path / "site.yaml"
    site.write_text("site_id: g4\nlabel: g4\n", encoding="utf-8")
    env_file = tmp_path / "field.env"
    env_file.write_text(f"FIELD_SITE={site}\nFIELD_TOKEN_FILE={token}\n", encoding="utf-8")

    process = subprocess.Popen(
        ["bash", str(FIELDCTL), "status"],
        env={
            "PATH": f"{binaries}{os.pathsep}/usr/bin{os.pathsep}/bin",
            "TMPDIR": str(scratch),
            "FIELD_ENV_FILE": str(env_file),
            "FIELD_TAILSCALE_IP": "100.90.110.54",
        },
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        # Long enough for the curl stub to be the foreground child.
        with pytest.raises(subprocess.TimeoutExpired):
            process.wait(timeout=3)
        process.terminate()
        process.wait(timeout=30)
    finally:
        if process.poll() is None:  # pragma: no cover - only on a hung stub
            process.kill()

    left = list(scratch.iterdir())
    assert left == [], f"scratch files survived: {left}"


# -- checks that passed when they could not see ---------------------------


def _api_active_job(body: str) -> subprocess.CompletedProcess[str]:
    script = "set -euo pipefail\n" + _shell_function("api_active_job") + "\napi_active_job\n"
    return subprocess.run(
        ["bash", "-c", script], input=body, capture_output=True, text=True, timeout=60, check=False
    )


@pytest.mark.parametrize(
    "body",
    [
        '{"jobs": [{"job_id":"j1","kind":"capture","status":"run',  # truncated mid-capture
        "<html><head><title>502 Bad Gateway</title></head></html>",
        "",
        "null",
        '{"jobs": {"j1": {"status": "running"}}}',
    ],
)
def test_an_unreadable_job_list_is_distinguishable_from_no_jobs(body: str) -> None:
    """Exit 1 means "nothing unfinished"; anything else must mean "I could
    not read this". They were both non-zero, and the caller read every
    non-zero as a green light -- so a truncated body that literally contained
    a running capture let the switch stop the service."""
    result = _api_active_job(body)

    assert result.returncode not in (0, 1), f"{body!r} was read as a definite answer"


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ('{"jobs": []}', 1),
        ('{"jobs": [{"job_id":"j1","kind":"capture","status":"succeeded"}]}', 1),
        ('{"jobs": [{"job_id":"j1","kind":"capture","status":"running"}]}', 0),
        ('{"jobs": [{"job_id":"j1","kind":"capture","status":"pending"}]}', 0),
    ],
)
def test_a_readable_job_list_still_answers_definitely(body: str, expected: int) -> None:
    assert _api_active_job(body).returncode == expected


def test_a_uid_that_cannot_be_read_is_not_treated_as_root(tmp_path: Path) -> None:
    """`id -u 2>/dev/null || echo 0` opened the privilege gate whenever `id`
    was missing -- the one direction this check must never fail in."""
    script = tmp_path / "probe.sh"
    script.write_text(
        "set -euo pipefail\n"
        "die() { printf 'fieldctl: %s\\n' \"$*\" >&2; exit 1; }\n"
        + _shell_function("require_root_for")
        + '\nrequire_root_for /etc/dmr-field/field.env.local campaign use day1 --write\n'
        'printf "PROCEEDED\\n"\n',
        encoding="utf-8",
    )

    result = subprocess.run(
        ["/bin/bash", str(script)],
        env={"PATH": str(tmp_path / "empty")},  # no `id` anywhere
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert result.returncode != 0, result.stdout
    assert "PROCEEDED" not in result.stdout
    assert "sudo fieldctl" in result.stderr


# -- the environment file, byte for byte ----------------------------------


def _local_env(tmp_path: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    script = "set -euo pipefail\n" + _shell_function("local_env") + '\nlocal_env "$@"\n'
    return subprocess.run(
        ["bash", "-c", script, "local_env", *arguments],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def test_every_occurrence_of_a_managed_key_is_rewritten(tmp_path: Path) -> None:
    """Both readers -- systemd and this script -- take the LAST assignment of
    a key. Rewriting only the first reported a switch that had not happened
    and left the service recording into the old campaign."""
    local = tmp_path / "field.env.local"
    local.write_text(
        "# operator's overrides\nFIELD_CAMPAIGN=day2\nKEEP=1\nFIELD_CAMPAIGN=day2\n",
        encoding="utf-8",
    )

    result = _local_env(tmp_path, "set", str(local), "PROJ", "day1")

    assert result.returncode == 0, result.stderr
    assignments = [
        line for line in local.read_text(encoding="utf-8").splitlines()
        if line.startswith("FIELD_CAMPAIGN=")
    ]
    assert assignments, "the key vanished"
    assert set(assignments) == {"FIELD_CAMPAIGN=day1"}, assignments


def test_a_crlf_file_keeps_its_line_endings(tmp_path: Path) -> None:
    """For systemd a trailing CR is part of the value, so rewriting a CRLF
    file as LF hands the service a different configuration -- and a rollback
    could never restore the bytes it started from."""
    local = tmp_path / "field.env.local"
    local.write_bytes(b"# note\r\nFIELD_CAMPAIGN=day1\r\nFIELD_HOST=10.0.0.1\r\n")

    result = _local_env(tmp_path, "set", str(local), "PROJ", "day2")

    assert result.returncode == 0, result.stderr
    after = local.read_bytes()
    assert b"FIELD_CAMPAIGN=day2\r\n" in after
    assert b"FIELD_HOST=10.0.0.1\r\n" in after, after


def test_a_line_break_python_invents_does_not_split_an_operators_line(
    tmp_path: Path,
) -> None:
    """`splitlines()` breaks on \\v, \\f, \\x85, \\u2028 and more, none of
    which systemd treats as a line break: one setting became two lines and
    its value was truncated."""
    local = tmp_path / "field.env.local"
    local.write_text("FIELD_LABEL=alpha\x0cbeta\nFIELD_CAMPAIGN=day1\n", encoding="utf-8")

    result = _local_env(tmp_path, "set", str(local), "PROJ", "day2")

    assert result.returncode == 0, result.stderr
    assert "FIELD_LABEL=alpha\x0cbeta" in local.read_text(encoding="utf-8")


def test_a_backup_and_restore_round_trip_is_byte_for_byte(tmp_path: Path) -> None:
    local = tmp_path / "field.env.local"
    original = b"# note\r\nFIELD_CAMPAIGN=day1\r\nKEEP=me\r\n"
    local.write_bytes(original)
    backup = tmp_path / "backup"

    assert _local_env(tmp_path, "backup", str(local), str(backup)).returncode == 0
    assert _local_env(tmp_path, "set", str(local), "PROJ", "day2").returncode == 0
    assert _local_env(tmp_path, "restore", str(local), str(backup)).returncode == 0

    assert local.read_bytes() == original


# -- a manifest the service can still read --------------------------------


def test_rewriting_a_manifest_keeps_its_mode(tmp_path: Path) -> None:
    """`campaign close` runs as root under /etc, and `mkstemp` creates at
    0600. The rename carried that onto the manifest, so the service user
    could no longer read the file it resolves at startup and the next restart
    failed on a file that had been readable a moment earlier."""
    manifest = tmp_path / "day1.yaml"
    manifest.write_text("schema_version: 1\n", encoding="utf-8")
    manifest.chmod(0o644)

    write_manifest_atomically(manifest, "schema_version: 1\nstatus: closed\n")

    assert stat.S_IMODE(manifest.stat().st_mode) == 0o644


def test_a_new_manifest_is_readable_by_the_service_user(tmp_path: Path) -> None:
    written = write_manifest_atomically(tmp_path / "new.yaml", "schema_version: 1\n")

    assert stat.S_IMODE(written.stat().st_mode) & stat.S_IROTH


def test_a_manifest_that_cannot_be_read_is_refused_with_a_reason(tmp_path: Path) -> None:
    """It used to escape `web serve`'s startup as an uncaught PermissionError
    rather than the refusal every other bad manifest gets."""
    manifest = tmp_path / "day1.yaml"
    manifest.write_text(
        "schema_version: 1\ncampaign_id: day1\nproject_id: p\nlabel: Day one\n",
        encoding="utf-8",
    )
    manifest.chmod(0o000)
    if os.geteuid() == 0:
        pytest.skip("root can read a mode-000 file, so the refusal cannot be provoked")

    with pytest.raises(ProjectError) as raised:
        load_campaign_manifest(manifest)

    assert "could not be read" in str(raised.value)


def test_closing_does_not_rewrite_characters_yaml_keeps_inside_a_value() -> None:
    """`splitlines()` split on U+2028 and rewrote an operator's label."""
    text = (
        "schema_version: 1\ncampaign_id: day3\nproject_id: p\n"
        'label: "Day 3 north leg"\ndefaults:\n  band: b\n'
    )

    closed = set_campaign_status_text(text, CAMPAIGN_STATUS_CLOSED)

    assert " north leg" in closed
    assert "status: closed" in closed


# -- a read-only listing that wrote a file --------------------------------


def test_listing_campaigns_cannot_create_a_database(tmp_path: Path) -> None:
    """A `#` in the database path started a URI fragment, truncating both the
    path and `?mode=ro`: a command documented as writing nothing opened a
    different file read-write and created it."""
    from dmr_iq_surveyor.cli_project import _runs_per_campaign

    database = tmp_path / "2026-09#day1.sqlite3"
    database.write_bytes(b"")
    before = sorted(path.name for path in tmp_path.iterdir())

    _runs_per_campaign(database)

    assert sorted(path.name for path in tmp_path.iterdir()) == before


# -- the receiver a radio actually reported -------------------------------


class SwigMapping:
    """SoapySDR's `Kwargs`: dict-like, convertible, not a `dict` subclass.

    `_soapy_probe.py` already converts its enumerate results with `dict(...)`
    for this reason.
    """

    def __init__(self, data: dict[str, str]) -> None:
        self._data = dict(data)

    def keys(self):  # noqa: ANN201 - mapping protocol
        return self._data.keys()

    def __getitem__(self, key: str) -> str:
        return self._data[key]


def test_the_serial_is_read_from_the_mapping_soapysdr_actually_returns() -> None:
    """An `isinstance(info, dict)` gate was False on the real radio, so the
    serial and label this feature exists to record were dropped on every
    capture -- invisibly, because no test without hardware could see it."""

    class Device:
        def getHardwareInfo(self) -> SwigMapping:  # noqa: N802 - SoapySDR's spelling
            return SwigMapping({"serial": "230405A498", "label": "SDRplay Dev0 RSP1A"})

        def getHardwareKey(self) -> str:  # noqa: N802 - SoapySDR's spelling
            return "RSP1A"

    assert device_identity(Device()) == {
        "serial": "230405A498",
        "label": "SDRplay Dev0 RSP1A",
    }


def test_a_drive_survives_a_device_that_reports_a_shape_the_store_refuses() -> None:
    """The live path merged the attribute unfiltered, so a non-scalar value
    built a blob the store rejected -- losing the whole drive rather than one
    reading -- and a non-mapping raised before the radio could be closed."""
    from dmr_iq_surveyor.survey.provenance import (
        hardware_provenance,
        normalise_hardware,
        scalar_identity,
    )

    for offered in ({"gains": {"IFGR": 20.0}}, "RSP1A", ["RSP1A"], None, 7):
        blob = hardware_provenance(
            identity={"driver": "sdrplay", **scalar_identity(offered)}
        )
        assert normalise_hardware(blob)["identity"]["driver"] == "sdrplay"


def _digest_module():
    """The digest script, imported the way `test_campaign_digest.py` does."""
    import importlib.util

    path = REPO_ROOT / "scripts" / "campaign_digest.py"
    spec = importlib.util.spec_from_file_location("campaign_digest_under_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_the_digest_counts_one_radio_once() -> None:
    """The counter keyed on `label + serial`, so one radio became two entries
    as soon as one stop got a label and another did not -- a false "a spare
    was swapped in" on a campaign run with a single receiver."""
    from dmr_iq_surveyor.survey.provenance import hardware_provenance

    receiver_name = _digest_module()._receiver_name
    answered = hardware_provenance(
        identity={"driver": "sdrplay", "serial": "230405A498", "label": "RSP1A"}
    )
    silent = hardware_provenance(identity={"driver": "sdrplay", "serial": "230405A498"})

    assert receiver_name(answered) == receiver_name(silent) == "230405A498"


def test_a_run_with_no_identity_does_not_silence_the_two_radio_warning() -> None:
    """`if "not recorded" not in observed` meant one legacy row suppressed
    the warning entirely -- the one case it exists for."""
    from collections import Counter

    counted = Counter({"A": 1, "B": 1, "not recorded": 1})
    named = [name for name in counted if name != "not recorded"]

    assert len(named) > 1


def test_a_blank_identity_is_not_a_receiver_name() -> None:
    name = _digest_module()._identity_name

    for blank in ("", "   ", None, False, 0, ["x"]):
        assert name(blank) is None


# -- a campaign closed under a running service ----------------------------


def test_a_running_service_stops_recording_into_a_campaign_closed_under_it(
    tmp_path: Path,
) -> None:
    """`require_open_campaign` runs once, at startup. Closing a campaign is
    something that happens to a RUNNING service -- and it kept accepting
    stops until the next restart, which then failed."""
    from dmr_iq_surveyor.web.service import FieldService, FieldSettings
    from dmr_iq_surveyor.web.viewscope import ReadOnlyViewError

    project_root = tmp_path / "proj"
    (project_root / "campaigns").mkdir(parents=True)
    manifest = project_root / "campaigns" / "day1.yaml"
    manifest.write_text(
        "schema_version: 1\ncampaign_id: day1\nproject_id: p25\nlabel: Day one\n",
        encoding="utf-8",
    )
    settings = FieldSettings(
        database_path=tmp_path / "db.sqlite3",
        recordings_dir=tmp_path / "rec",
        output_root=tmp_path / "out",
        campaign_id="day1",
        project_id="p25",
        project_root=project_root,
    )
    service = FieldService(settings)

    service.refuse_if_campaign_closed("recording a stop")  # open: no refusal

    manifest.write_text(
        set_campaign_status_text(manifest.read_text(encoding="utf-8"), CAMPAIGN_STATUS_CLOSED),
        encoding="utf-8",
    )

    with pytest.raises(ReadOnlyViewError) as raised:
        service.refuse_if_campaign_closed("recording a stop")
    assert "closed" in str(raised.value)


def test_a_deployment_with_no_project_is_unaffected_by_the_runtime_check(
    tmp_path: Path,
) -> None:
    """No project means no manifest to consult, and the service must behave
    exactly as it did before this check existed."""
    from dmr_iq_surveyor.web.service import FieldService, FieldSettings

    service = FieldService(
        FieldSettings(
            database_path=tmp_path / "db.sqlite3",
            recordings_dir=tmp_path / "rec",
            output_root=tmp_path / "out",
            campaign_id="day1",
        )
    )

    service.refuse_if_campaign_closed("recording a stop")


def test_a_manifest_that_vanished_does_not_take_the_running_service_down(
    tmp_path: Path,
) -> None:
    """Refusing on an unreadable manifest would stop a working deployment
    over a path that moved; the campaign was proven open at startup."""
    from dmr_iq_surveyor.web.service import FieldService, FieldSettings

    service = FieldService(
        FieldSettings(
            database_path=tmp_path / "db.sqlite3",
            recordings_dir=tmp_path / "rec",
            output_root=tmp_path / "out",
            campaign_id="day1",
            project_id="p25",
            project_root=tmp_path / "gone",
        )
    )

    service.refuse_if_campaign_closed("recording a stop")
