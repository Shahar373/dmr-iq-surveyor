"""`scripts/fieldctl` and the systemd unit read the same configuration.

The unit names two environment files -- `/etc/dmr-field/field.env` and then
`-/etc/dmr-field/field.env.local` -- and systemd applies them in that order,
so the local file wins. `fieldctl` read only the first, which made every
command an operator runs to *check* the service report on a configuration the
service is not running with: `fieldctl status` would say "no project" while
the running app was recording into a campaign, and `fieldctl print-command`
would print an argv missing the very flags the service was started with.

These tests run the real script through `bash`, the way
`tests/test_fieldctl_command.py` does, and stay hermetic the same way:
`FIELD_ENV_FILE` points inside `tmp_path`, so neither a real `/etc/dmr-field`
nor a developer's environment can reach them. Because the local path is
derived from `FIELD_ENV_FILE` rather than hardcoded, that one override
isolates both files.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
FIELDCTL = REPO_ROOT / "scripts" / "fieldctl"
UNIT = REPO_ROOT / "deploy" / "dmr-field.service"


def _stub_bin(tmp_path: Path) -> Path:
    directory = tmp_path / "bin"
    directory.mkdir(exist_ok=True)
    for name in ("sudo", "systemctl", "tailscale", "journalctl"):
        stub = directory / name
        stub.write_text(
            f'#!/bin/sh\necho "$@" >> "{tmp_path}/{name}.called"\nexit 0\n',
            encoding="utf-8",
        )
        stub.chmod(0o755)
    return directory


def _surveyor_stub(tmp_path: Path) -> Path:
    """Stands in for `dmr-surveyor`, recording the argv it was exec'd with.

    `fieldctl exec` really does exec this, which is the only way to show that
    the argv the service ends up running is the one `print-command` printed.
    """
    stub = tmp_path / "dmr-surveyor"
    stub.write_text(
        f'#!/bin/sh\nfor argument in "$@"; do printf "%s\\n" "$argument"; '
        f'done > "{tmp_path}/exec.argv"\nexit 0\n',
        encoding="utf-8",
    )
    stub.chmod(0o755)
    return stub


def _config(tmp_path: Path, base: str = "", local: str | None = None, **overrides: str):
    """An environment whose base and local files are both inside tmp_path."""
    site = tmp_path / "field_site.yaml"
    site.write_text("site_id: g4\nlabel: g4\n", encoding="utf-8")
    token = tmp_path / "token"
    token.write_text("s3cret-token-value\n", encoding="utf-8")
    token.chmod(0o600)

    env_file = tmp_path / "field.env"
    env_file.write_text(f"FIELD_SITE={site}\nFIELD_TOKEN_FILE={token}\n{base}", encoding="utf-8")
    if local is not None:
        (tmp_path / "field.env.local").write_text(local, encoding="utf-8")

    environment = {
        "PATH": f"{_stub_bin(tmp_path)}{os.pathsep}/usr/bin{os.pathsep}/bin",
        "FIELD_ENV_FILE": str(env_file),
        "FIELD_TAILSCALE_IP": "100.90.110.54",
        "FIELD_SURVEYOR_BIN": str(_surveyor_stub(tmp_path)),
    }
    environment.update(overrides)
    return environment


def _run(environment: dict[str, str], *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(FIELDCTL), *arguments],
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
        cwd=REPO_ROOT,
        check=False,
    )


def _argv(environment: dict[str, str]) -> list[str]:
    result = _run(environment, "print-command")
    assert result.returncode == 0, result.stderr
    return result.stdout.splitlines()


# -- the two files, in the unit's order -----------------------------------


def test_a_value_set_only_in_the_local_file_reaches_the_argv(tmp_path: Path) -> None:
    """The case from the field: the campaign lives in `field.env.local` so an
    update can replace `field.env` without discarding it."""
    environment = _config(
        tmp_path,
        local="FIELD_PROJECT=/etc/dmr-field/projects/p25/project.yaml\n"
        "FIELD_CAMPAIGN=2026-09_day1\n",
    )

    argv = _argv(environment)

    assert argv[argv.index("--campaign") + 1] == "2026-09_day1"
    assert argv[argv.index("--project") + 1] == "/etc/dmr-field/projects/p25/project.yaml"


def test_the_local_file_wins_over_the_base_file(tmp_path: Path) -> None:
    environment = _config(
        tmp_path,
        base="FIELD_CAMPAIGN=2026-09_day1\nFIELD_PORT=8765\n",
        local="FIELD_CAMPAIGN=2026-09_day2\nFIELD_PORT=9999\n",
    )

    argv = _argv(environment)

    assert argv[argv.index("--campaign") + 1] == "2026-09_day2"
    assert argv[argv.index("--port") + 1] == "9999"


def test_the_local_file_can_clear_a_value_the_base_file_sets(tmp_path: Path) -> None:
    """`FIELD_CAMPAIGN=` in the local file is how systemd is told to stop
    recording into a campaign. Treating an empty assignment as "nothing to
    say" put the base file's campaign back, and the service and the wrapper
    then disagreed about which round was being recorded."""
    environment = _config(
        tmp_path,
        base="FIELD_CAMPAIGN=2026-09_day1\n",
        local="FIELD_CAMPAIGN=\n",
    )

    argv = _argv(environment)

    assert "--campaign" not in argv
    assert "2026-09_day1" not in argv


def test_a_missing_local_file_is_normal_rather_than_an_error(tmp_path: Path) -> None:
    """The unit's `-` prefix says so, and no installer has ever created one."""
    environment = _config(tmp_path, base="FIELD_CAMPAIGN=2026-09_day1\n")
    assert not (tmp_path / "field.env.local").exists()

    argv = _argv(environment)

    assert argv[argv.index("--campaign") + 1] == "2026-09_day1"


def test_the_environment_still_beats_both_files(tmp_path: Path) -> None:
    """A one-off override for a single invocation, which is what this
    wrapper has always offered and what the test suite itself relies on."""
    environment = _config(
        tmp_path,
        base="FIELD_CAMPAIGN=2026-09_day1\n",
        local="FIELD_CAMPAIGN=2026-09_day2\n",
        FIELD_CAMPAIGN="2026-09_day3",
    )

    assert _argv(environment)[_argv(environment).index("--campaign") + 1] == "2026-09_day3"


# -- the commands that report agree with the command that runs ------------


def test_print_command_status_and_exec_all_see_the_same_campaign(
    tmp_path: Path,
) -> None:
    """The whole point. `exec` is what systemd runs; `print-command` is what
    an operator checks before restarting; `status` is what they paste into a
    chat when something looks wrong. A campaign set only in the local file
    has to reach all three."""
    environment = _config(
        tmp_path,
        local="FIELD_PROJECT=/etc/dmr-field/projects/p25/project.yaml\n"
        "FIELD_CAMPAIGN=2026-09_day1\n",
    )

    printed = _argv(environment)

    executed = _run(environment, "exec")
    assert executed.returncode == 0, executed.stderr
    recorded = (tmp_path / "exec.argv").read_text(encoding="utf-8").splitlines()

    status = _run(environment, "status")
    assert status.returncode == 0, status.stderr

    # argv[0] is the binary itself, which `exec` becomes rather than passes.
    assert recorded == printed[1:]
    assert "2026-09_day1" in status.stdout
    assert "/etc/dmr-field/projects/p25/project.yaml" in status.stdout


def test_status_names_the_file_each_value_came_from(tmp_path: Path) -> None:
    """With two files, "where is this set" is its own question, and the
    answer decides which file the operator edits."""
    environment = _config(
        tmp_path,
        base="FIELD_PROJECT=/etc/dmr-field/projects/p25/project.yaml\n",
        local="FIELD_CAMPAIGN=2026-09_day1\n",
    )

    result = _run(environment, "status")

    assert result.returncode == 0, result.stderr
    assert re.search(r"project\s+\S+\s+\[base\]", result.stdout), result.stdout
    assert re.search(r"campaign\s+2026-09_day1\s+\[local\]", result.stdout), result.stdout


def test_status_reports_the_local_file_as_absent_rather_than_silently(
    tmp_path: Path,
) -> None:
    result = _run(_config(tmp_path), "status")

    assert result.returncode == 0, result.stderr
    assert "field.env.local absent" in result.stdout


def test_a_campaign_without_a_project_is_reported_as_tagged_not_as_lost(
    tmp_path: Path,
) -> None:
    """The warning used to say stops "stay unassigned", and that was wrong:
    `web serve` passes `--campaign` whether or not `--project` is given, so
    the id does reach `survey_runs.campaign_id`. What is missing is the
    manifest, and that is what the campaign commands need."""
    environment = _config(tmp_path, local="FIELD_CAMPAIGN=2026-09_day1\n")

    argv = _argv(environment)
    result = _run(environment, "status")

    assert argv[argv.index("--campaign") + 1] == "2026-09_day1"
    assert "--project" not in argv
    assert "WARNING" in result.stdout
    assert "still tagged" in result.stdout


# -- drift against the unit -----------------------------------------------


def _environment_files() -> list[str]:
    """The unit's `EnvironmentFile=` values, in the order systemd applies
    them."""
    return [
        line.split("=", 1)[1].strip()
        for line in UNIT.read_text(encoding="utf-8").splitlines()
        if line.strip().startswith("EnvironmentFile=")
    ]


def test_the_unit_reads_exactly_the_two_files_fieldctl_reads() -> None:
    """The drift test for this pair. A third file in the unit, a renamed one,
    or a change of order would put the service and the wrapper back out of
    step -- silently, because both would still work."""
    assert _environment_files() == [
        "/etc/dmr-field/field.env",
        "-/etc/dmr-field/field.env.local",
    ]


def test_fieldctl_defaults_to_the_same_pair_of_paths(tmp_path: Path) -> None:
    """Asserted from the script's own reported paths rather than from its
    source, so it is the behaviour that is pinned."""
    environment = {
        "PATH": f"{_stub_bin(tmp_path)}{os.pathsep}/usr/bin{os.pathsep}/bin",
        "FIELD_TAILSCALE_IP": "100.90.110.54",
    }

    result = _run(environment, "status")

    base, local = (name.lstrip("-") for name in _environment_files())
    assert f"env      {base}\n" in result.stdout
    assert f"env      {local} absent" in result.stdout or f"env      {local} (" in result.stdout


def test_the_optional_local_file_is_the_one_with_the_skip_if_absent_prefix() -> None:
    """`-` means "skip if absent". On the base file it would turn a missing
    configuration into a service that starts with none of it."""
    base, local = _environment_files()
    assert not base.startswith("-")
    assert local.startswith("-")
