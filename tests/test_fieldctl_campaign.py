"""`fieldctl campaign`: switching what the Pi records into, without losing a stop.

Changing the campaign on a deployed Pi used to be "edit `field.env` by hand,
then `fieldctl restart`". Three things could go wrong quietly: the restart
abandoned a capture that was still running, a typo left the service refusing
to start with no way back, and nothing ever checked that the service actually
came up recording into the campaign that had been asked for.

These tests run the real script through `bash`. Nothing real is reached:
stubs on PATH stand in for `systemctl`, `curl` and `id`, and every path lives
under `tmp_path`, so no service is controlled, no API is called and no
`/etc/dmr-field` is read. The stubs record what they were asked to do, which
is how the ordering assertions below are made -- the job check before the
stop, the stop before the write, the write before the start.

`id` is stubbed because the refusal to write under `/etc` without root is
decided by the uid, and the suite has to assert both answers whether it is
run as root or not.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
FIELDCTL = REPO_ROOT / "scripts" / "fieldctl"

TOKEN_VALUE = "s3cret-token-value"

PROJECT_YAML = """\
schema_version: 1
project_id: p25_central_il
label: P25 central Israel
analyzer: p25_site_geolocation
database: db.sqlite3
defaults:
  band: central_800_narrow
"""

CAMPAIGN_YAML = """\
# The operator's own note, which nothing here may drop.
schema_version: 1
campaign_id: {campaign_id}
project_id: p25_central_il
label: {label}
defaults:
  hardware: rsp1a_field
  capture:
    sample_rate_hz: 768000
    duration_seconds: 30
"""


class Deployment:
    """A pretend Pi: an environment file pair, a project, and stubs."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.bin = root / "bin"
        self.etc = root / "etc"
        self.project_root = root / "proj"
        for directory in (self.bin, self.etc, self.project_root / "campaigns"):
            directory.mkdir(parents=True, exist_ok=True)

        (self.project_root / "project.yaml").write_text(PROJECT_YAML, encoding="utf-8")
        site = root / "site.yaml"
        site.write_text("site_id: g4\nlabel: g4\n", encoding="utf-8")
        token = root / "token"
        token.write_text(TOKEN_VALUE + "\n", encoding="utf-8")
        token.chmod(0o600)

        self.env_file = self.etc / "field.env"
        self.env_file.write_text(
            "\n".join(
                [
                    f"FIELD_SITE={site}",
                    f"FIELD_TOKEN_FILE={token}",
                    f"FIELD_SURVEYOR_BIN={self._surveyor()}",
                    f"FIELD_PROJECT={self.project_root / 'project.yaml'}",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        self.local_file = self.etc / "field.env.local"
        self._write_stubs()
        self.set_service("inactive")
        self.set_jobs([])

    @staticmethod
    def _surveyor() -> str:
        """The dmr-surveyor of whatever interpreter is running the suite.

        `fieldctl` delegates every manifest read and write to it, which is the
        point: the schema, the id validator and the atomic write have exactly
        one implementation.
        """
        import shutil
        import sys

        found = shutil.which("dmr-surveyor", path=str(Path(sys.executable).parent))
        return found or str(Path(sys.executable).parent / "dmr-surveyor")

    def _write_stubs(self) -> None:
        (self.bin / "systemctl").write_text(
            f'''#!/bin/sh
echo "$@" >> "{self.root}/systemctl.called"
case "$1 $2" in
  "is-active dmr-field.service") cat "{self.root}/service.state" ;;
esac
exit 0
''',
            encoding="utf-8",
        )
        (self.bin / "curl").write_text(
            f'''#!/bin/sh
echo "$@" >> "{self.root}/curl.called"
for argument in "$@"; do
  case "$argument" in
    */api/jobs)  cat "{self.root}/api.jobs" 2>/dev/null || exit 7; exit 0 ;;
    */api/state) cat "{self.root}/api.state" 2>/dev/null || exit 7; exit 0 ;;
  esac
done
exit 7
''',
            encoding="utf-8",
        )
        self.set_uid(0)
        for name in ("sudo", "tailscale", "journalctl"):
            (self.bin / name).write_text(
                f'#!/bin/sh\necho "$@" >> "{self.root}/{name}.called"\nexit 0\n',
                encoding="utf-8",
            )
        for stub in self.bin.iterdir():
            stub.chmod(0o755)

    # -- the state the stubs report --------------------------------------

    def set_uid(self, uid: int) -> None:
        (self.bin / "id").write_text(
            f'#!/bin/sh\nif [ "$1" = "-u" ]; then echo {uid}; exit 0; fi\nexit 0\n',
            encoding="utf-8",
        )
        (self.bin / "id").chmod(0o755)

    def set_service(self, state: str) -> None:
        (self.root / "service.state").write_text(state + "\n", encoding="utf-8")

    def set_jobs(self, jobs: list[dict[str, str]]) -> None:
        (self.root / "api.jobs").write_text(json.dumps({"jobs": jobs}), encoding="utf-8")

    def set_api_campaign(self, campaign_id: str | None, project_id: str = "p25_central_il") -> None:
        (self.root / "api.state").write_text(
            json.dumps(
                {"scope": {"project_id": project_id, "capture_campaign_id": campaign_id}}
            ),
            encoding="utf-8",
        )

    def unreachable_api(self) -> None:
        (self.root / "api.state").unlink(missing_ok=True)
        (self.root / "api.jobs").unlink(missing_ok=True)

    # -- the deployment's own files --------------------------------------

    def campaign(self, campaign_id: str, *, label: str | None = None, status: str | None = None) -> Path:
        body = CAMPAIGN_YAML.format(campaign_id=campaign_id, label=label or campaign_id)
        if status is not None:
            body += f"status: {status}\n"
        path = self.project_root / "campaigns" / f"{campaign_id}.yaml"
        path.write_text(body, encoding="utf-8")
        return path

    def set_local(self, text: str) -> None:
        self.local_file.write_text(text, encoding="utf-8")

    def calls(self, name: str) -> list[str]:
        path = self.root / f"{name}.called"
        if not path.exists():
            return []
        return [line for line in path.read_text(encoding="utf-8").splitlines() if line]

    def forget_calls(self) -> None:
        for name in ("systemctl", "curl", "sudo"):
            (self.root / f"{name}.called").unlink(missing_ok=True)

    def run(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        environment = {
            "PATH": f"{self.bin}{os.pathsep}/usr/bin{os.pathsep}/bin",
            "HOME": str(self.root),
            "FIELD_ENV_FILE": str(self.env_file),
            "FIELD_TAILSCALE_IP": "100.90.110.54",
            # The unit's own startup budget bounds how long a verification
            # waits; zero keeps the suite quick without changing the logic.
            "FIELD_TAILSCALE_WAIT": "0",
            # Rich wraps to the terminal width, and a wrapped path is one no
            # assertion can find.
            "COLUMNS": "200",
        }
        return subprocess.run(
            ["bash", str(FIELDCTL), *arguments],
            env=environment,
            capture_output=True,
            text=True,
            timeout=120,
            cwd=REPO_ROOT,
            check=False,
        )


@pytest.fixture()
def pi(tmp_path: Path) -> Deployment:
    deployment = Deployment(tmp_path)
    deployment.campaign("day1", label="Day one")
    return deployment


# -- reading -------------------------------------------------------------


def test_list_needs_no_privileges_and_touches_no_service(pi: Deployment) -> None:
    pi.campaign("day2", label="Day two")

    result = pi.run("campaign", "list")

    assert result.returncode == 0, result.stderr
    assert "day1" in result.stdout
    assert "day2" in result.stdout
    assert "open" in result.stdout
    assert pi.calls("sudo") == []
    assert pi.calls("systemctl") == []


def test_list_shows_the_lifecycle_and_which_campaign_is_current(pi: Deployment) -> None:
    pi.campaign("day2", label="Day two", status="closed")
    pi.set_local("FIELD_CAMPAIGN=day1\n")

    result = pi.run("campaign", "list")

    lines = {line.split()[0]: line for line in result.stdout.splitlines() if line[:3] == "day"}
    assert "open" in lines["day1"] and "yes" in lines["day1"]
    assert "closed" in lines["day2"] and "yes" not in lines["day2"]


def test_an_unreadable_manifest_is_a_row_not_a_traceback(pi: Deployment) -> None:
    """One broken manifest among twenty is exactly when this listing is
    needed most, and a traceback would take the other nineteen away."""
    (pi.project_root / "campaigns" / "broken.yaml").write_text(
        "schema_version: 1\ncampaign_id: broken\nproject_id: p25_central_il\n"
        "label: Broken\nnonsense_key: 1\n",
        encoding="utf-8",
    )

    result = pi.run("campaign", "list")

    assert result.returncode == 0, result.stderr
    assert "day1" in result.stdout
    assert "PROBLEM" in result.stdout
    assert "Traceback" not in result.stdout + result.stderr


def test_current_names_the_file_the_campaign_came_from(pi: Deployment) -> None:
    pi.set_local("FIELD_CAMPAIGN=day1\n")

    result = pi.run("campaign", "current")

    assert result.returncode == 0, result.stderr
    assert "day1" in result.stdout
    assert "[local]" in result.stdout
    assert "status   open" in result.stdout


def test_current_warns_when_the_running_service_disagrees_with_the_file(
    pi: Deployment,
) -> None:
    """The configuration was edited and the service was never restarted.
    Stops are going to the campaign the process holds, not the one on disk,
    and nothing else says so."""
    pi.campaign("day2", label="Day two")
    pi.set_local("FIELD_CAMPAIGN=day2\n")
    pi.set_service("active")
    pi.set_api_campaign("day1")

    result = pi.run("campaign", "current")

    assert "WARNING" in result.stdout
    assert "day1" in result.stdout and "day2" in result.stdout


def test_current_still_reports_when_the_launch_command_cannot_be_assembled(
    pi: Deployment,
) -> None:
    """Which campaign is current is exactly what an operator needs to know
    while something else is broken. A missing site profile, or a tailnet that
    is down, stops the argv being assembled -- it must not stop the report."""
    pi.set_local(f"FIELD_CAMPAIGN=day1\nFIELD_SITE={pi.root}/absent.yaml\n")

    result = pi.run("campaign", "current")

    assert result.returncode == 0, result.stderr
    assert "day1" in result.stdout
    assert "status   open" in result.stdout
    assert "could not be assembled" in result.stdout


def test_neither_reading_command_ever_prints_the_token_or_a_bookmark(
    pi: Deployment,
) -> None:
    pi.set_local("FIELD_CAMPAIGN=day1\n")
    pi.set_service("active")
    pi.set_api_campaign("day1")

    for command in (("campaign", "list"), ("campaign", "current")):
        result = pi.run(*command)
        assert TOKEN_VALUE not in result.stdout
        assert TOKEN_VALUE not in result.stderr
        assert "?token=" not in result.stdout


# -- dry runs ------------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [("campaign", "use", "day2"), ("campaign", "close", "day2"), ("campaign", "new", "day3")],
)
def test_a_dry_run_writes_nothing_and_neither_stops_nor_starts_anything(
    pi: Deployment, command: tuple[str, ...]
) -> None:
    """The default for every command that changes something. An operator is
    told to look at the report first, and a report that had already acted
    would make that advice a lie."""
    pi.campaign("day2", label="Day two")
    pi.set_local("FIELD_CAMPAIGN=day1\n")
    before = pi.local_file.read_text(encoding="utf-8")
    manifests = {
        path: path.read_text(encoding="utf-8")
        for path in (pi.project_root / "campaigns").glob("*.yaml")
    }

    result = pi.run(*command)

    assert result.returncode == 0, result.stderr
    assert pi.local_file.read_text(encoding="utf-8") == before
    assert {
        path: path.read_text(encoding="utf-8")
        for path in (pi.project_root / "campaigns").glob("*.yaml")
    } == manifests
    assert not (pi.project_root / "campaigns" / "day3.yaml").exists()
    for call in pi.calls("systemctl"):
        assert call.startswith("is-active") or call.startswith("show"), call


def test_the_dry_run_of_a_switch_names_the_file_and_the_steps(pi: Deployment) -> None:
    pi.campaign("day2", label="Day two")
    pi.set_local("FIELD_CAMPAIGN=day1\n")
    pi.set_service("active")

    result = pi.run("campaign", "use", "day2")

    assert "current  day1" in result.stdout
    assert "target   day2" in result.stdout
    assert str(pi.local_file) in result.stdout
    assert str(pi.env_file) in result.stdout
    assert "left untouched" in result.stdout


# -- refusals before anything changes ------------------------------------


def test_writing_without_root_refuses_and_prints_the_sudo_line(pi: Deployment) -> None:
    pi.campaign("day2", label="Day two")
    pi.set_local("FIELD_CAMPAIGN=day1\n")
    pi.set_uid(1000)
    before = pi.local_file.read_text(encoding="utf-8")

    result = pi.run("campaign", "use", "day2", "--write")

    assert result.returncode != 0
    assert "needs root" in result.stderr
    assert "sudo fieldctl campaign use day2 --write" in result.stderr
    assert pi.local_file.read_text(encoding="utf-8") == before
    assert pi.calls("sudo") == [], "fieldctl must not call sudo for you"
    assert [call for call in pi.calls("systemctl") if not call.startswith("is-active")] == []


def test_switching_to_a_closed_campaign_is_refused(pi: Deployment) -> None:
    pi.campaign("day2", label="Day two", status="closed")
    pi.set_local("FIELD_CAMPAIGN=day1\n")

    result = pi.run("campaign", "use", "day2", "--write")

    assert result.returncode != 0
    assert "closed" in result.stderr
    assert "readable and analysable" in result.stderr
    assert "FIELD_CAMPAIGN=day1" in pi.local_file.read_text(encoding="utf-8")


def test_switching_to_a_campaign_the_project_does_not_declare_is_refused(
    pi: Deployment,
) -> None:
    result = pi.run("campaign", "use", "never_declared", "--write")

    assert result.returncode != 0
    assert "not a campaign of project" in result.stderr


def test_switching_to_the_campaign_already_in_use_is_a_stated_no_op(
    pi: Deployment,
) -> None:
    """`set_api_campaign` is what makes this a no-op rather than a refusal.

    "Already the campaign this deployment records into" is a claim about the
    running service, and a service reads its configuration once, at start --
    so the file alone cannot support it, and the API has to agree before the
    claim is made. Without this line the deployment is one whose API cannot
    be read, which `test_fieldctl_service_recovery.py` covers and which is
    refused.
    """
    pi.set_local("FIELD_CAMPAIGN=day1\n")
    pi.set_service("active")
    pi.set_api_campaign("day1")

    result = pi.run("campaign", "use", "day1", "--write")

    assert result.returncode == 0, result.stderr
    assert "already the campaign" in result.stderr
    assert [call for call in pi.calls("systemctl") if not call.startswith("is-active")] == []


def test_a_symlinked_local_file_is_refused_before_the_service_is_stopped(
    pi: Deployment,
) -> None:
    """Writing through the link would replace it with a regular file and
    leave its target untouched -- and discovering that after the stop would
    fail the restore for the same reason, leaving the service down."""
    pi.campaign("day2", label="Day two")
    target = pi.root / "elsewhere.env"
    target.write_text("FIELD_CAMPAIGN=day1\n", encoding="utf-8")
    pi.local_file.symlink_to(target)
    pi.set_service("active")

    result = pi.run("campaign", "use", "day2", "--write")

    assert result.returncode != 0
    assert "symbolic link" in result.stderr
    assert pi.local_file.is_symlink()
    assert target.read_text(encoding="utf-8") == "FIELD_CAMPAIGN=day1\n"
    assert "stop dmr-field.service" not in pi.calls("systemctl")


def test_closing_the_campaign_being_recorded_into_is_refused(pi: Deployment) -> None:
    """It would leave the service pointed at a campaign it may no longer
    write to, and it would not find out until its next start -- which is a
    restart nobody planned, at the side of a road."""
    pi.set_local("FIELD_CAMPAIGN=day1\n")

    result = pi.run("campaign", "close", "day1", "--write")

    assert result.returncode != 0
    assert "Switch to another campaign first" in result.stderr
    assert "status" not in (pi.project_root / "campaigns" / "day1.yaml").read_text(
        encoding="utf-8"
    )


# -- the job check and the order of operations ---------------------------


def test_a_job_that_has_not_finished_refuses_the_switch(pi: Deployment) -> None:
    pi.campaign("day2", label="Day two")
    pi.set_local("FIELD_CAMPAIGN=day1\n")
    pi.set_service("active")
    pi.set_jobs([{"job_id": "a1", "kind": "capture", "status": "running"}])

    result = pi.run("campaign", "use", "day2", "--write")

    assert result.returncode != 0
    assert "has not finished" in result.stderr
    assert "FIELD_CAMPAIGN=day1" in pi.local_file.read_text(encoding="utf-8")
    assert "stop dmr-field.service" not in pi.calls("systemctl")


@pytest.mark.parametrize("status", ["succeeded", "failed", "cancelled"])
def test_a_finished_job_does_not_block_the_switch(pi: Deployment, status: str) -> None:
    """Terminal is the job model's own word. Reading it the other way round
    -- only "running" counts -- would abandon a capture that has been
    submitted and has not started yet, so the test pins both directions."""
    pi.campaign("day2", label="Day two")
    pi.set_local("FIELD_CAMPAIGN=day1\n")
    pi.set_service("active")
    pi.set_jobs([{"job_id": "a1", "kind": "capture", "status": status}])
    pi.set_api_campaign("day2")

    result = pi.run("campaign", "use", "day2", "--write")

    assert result.returncode == 0, result.stderr + result.stdout
    assert "FIELD_CAMPAIGN=day2" in pi.local_file.read_text(encoding="utf-8")


def test_a_pending_job_counts_as_unfinished(pi: Deployment) -> None:
    pi.campaign("day2", label="Day two")
    pi.set_local("FIELD_CAMPAIGN=day1\n")
    pi.set_service("active")
    pi.set_jobs([{"job_id": "a1", "kind": "capture", "status": "pending"}])

    result = pi.run("campaign", "use", "day2", "--write")

    assert result.returncode != 0
    assert "has not finished" in result.stderr


def test_the_service_is_stopped_before_the_file_changes(pi: Deployment) -> None:
    """The race this closes: between "no job is running" and the new campaign
    taking effect, the app is still up and its Record button still works. A
    stop taken after the check and before the write is what makes the answer
    still true when the file changes."""
    pi.campaign("day2", label="Day two")
    pi.set_local("FIELD_CAMPAIGN=day1\n")
    pi.set_service("active")
    pi.set_api_campaign("day2")

    result = pi.run("campaign", "use", "day2", "--write")

    assert result.returncode == 0, result.stderr
    calls = [call for call in pi.calls("systemctl") if not call.startswith("is-active")]
    assert calls[0].startswith("stop"), calls
    assert any(call.startswith("start") for call in calls), calls
    asked = pi.calls("curl")
    assert any("/api/jobs" in call for call in asked), asked


def test_the_switch_refuses_when_the_running_service_cannot_be_asked_about_jobs(
    pi: Deployment,
) -> None:
    """A service that is up but not answering may be mid-capture. Restarting
    it blind is the thing this command exists to stop doing."""
    pi.campaign("day2", label="Day two")
    pi.set_local("FIELD_CAMPAIGN=day1\n")
    pi.set_service("active")
    pi.unreachable_api()

    result = pi.run("campaign", "use", "day2", "--write")

    assert result.returncode != 0
    assert "could not be reached" in result.stderr
    assert "stop dmr-field.service" not in pi.calls("systemctl")


# -- writing, verifying, and rolling back --------------------------------


def test_a_successful_switch_verifies_the_campaign_through_the_api(
    pi: Deployment,
) -> None:
    """`systemctl start` returning 0 says a process was launched, not that it
    came up recording into the campaign that was asked for."""
    pi.campaign("day2", label="Day two")
    pi.set_local("FIELD_CAMPAIGN=day1\n")
    pi.set_service("active")
    pi.set_api_campaign("day2")

    result = pi.run("campaign", "use", "day2", "--write")

    assert result.returncode == 0, result.stderr
    assert "campaign day2" in result.stdout
    assert any("/api/state" in call for call in pi.calls("curl"))


def test_every_other_line_of_the_local_file_survives_a_switch(pi: Deployment) -> None:
    """Local overrides are what this file is for, and a campaign switch is
    not an invitation to reformat them."""
    pi.campaign("day2", label="Day two")
    pi.set_local(
        "# hotspot for the coastal run\n"
        "FIELD_HOST=100.90.110.54\n"
        "FIELD_CAMPAIGN=day1\n"
        "\n"
        "# keep the recordings on this leg\n"
        "FIELD_KEEP_RECORDINGS=3\n"
    )
    pi.set_service("active")
    pi.set_api_campaign("day2")

    result = pi.run("campaign", "use", "day2", "--write")

    assert result.returncode == 0, result.stderr
    after = pi.local_file.read_text(encoding="utf-8").splitlines()
    assert "# hotspot for the coastal run" in after
    assert "FIELD_HOST=100.90.110.54" in after
    assert "# keep the recordings on this leg" in after
    assert "FIELD_KEEP_RECORDINGS=3" in after
    assert "FIELD_CAMPAIGN=day2" in after
    assert "FIELD_CAMPAIGN=day1" not in after


def test_a_missing_local_file_is_created_without_touching_the_base_file(
    pi: Deployment,
) -> None:
    pi.campaign("day2", label="Day two")
    base = pi.env_file.read_text(encoding="utf-8")
    pi.set_api_campaign("day2")

    result = pi.run("campaign", "use", "day2", "--write")

    assert result.returncode == 0, result.stderr
    assert "FIELD_CAMPAIGN=day2" in pi.local_file.read_text(encoding="utf-8")
    assert pi.env_file.read_text(encoding="utf-8") == base


def test_a_service_that_comes_up_on_the_wrong_campaign_is_rolled_back(
    pi: Deployment,
) -> None:
    """The whole reason the verification exists. The file is restored byte
    for byte, the service is started again, and the command exits non-zero
    saying which campaign is actually in force."""
    pi.campaign("day2", label="Day two")
    original = "# do not lose me\nFIELD_CAMPAIGN=day1\n"
    pi.set_local(original)
    pi.set_service("active")
    pi.set_api_campaign("day1")  # the restart does not take the new campaign

    result = pi.run("campaign", "use", "day2", "--write")

    assert result.returncode != 0
    assert pi.local_file.read_text(encoding="utf-8") == original
    assert "Rolled back" in result.stderr
    starts = [call for call in pi.calls("systemctl") if call.startswith("start")]
    assert len(starts) == 2, "the service must be started again by the rollback"


def test_a_rollback_removes_a_local_file_that_did_not_exist_before(
    pi: Deployment,
) -> None:
    """Restoring "as it was" means there is no file afterwards either -- not
    an empty one, and not one naming a campaign nobody chose."""
    pi.campaign("day2", label="Day two")
    pi.set_service("active")
    pi.set_api_campaign(None)  # comes up with no campaign at all

    result = pi.run("campaign", "use", "day2", "--write")

    assert result.returncode != 0
    assert not pi.local_file.exists()


def test_a_service_that_fails_to_start_is_rolled_back_without_waiting_it_out(
    pi: Deployment,
) -> None:
    pi.campaign("day2", label="Day two")
    original = "FIELD_CAMPAIGN=day1\n"
    pi.set_local(original)
    pi.set_service("active")
    pi.set_api_campaign("day2")

    # The unit reports `failed` once it is started, and the API never answers.
    (pi.bin / "systemctl").write_text(
        f'''#!/bin/sh
echo "$@" >> "{pi.root}/systemctl.called"
case "$1 $2" in
  "is-active dmr-field.service")
      if [ -f "{pi.root}/started" ]; then echo failed; else cat "{pi.root}/service.state"; fi ;;
  "start dmr-field.service") touch "{pi.root}/started" ;;
esac
exit 0
''',
        encoding="utf-8",
    )
    (pi.bin / "systemctl").chmod(0o755)
    (pi.bin / "curl").write_text(
        f'#!/bin/sh\necho "$@" >> "{pi.root}/curl.called"\n'
        f'[ -f "{pi.root}/started" ] && exit 7\ncat "{pi.root}/api.jobs"\nexit 0\n',
        encoding="utf-8",
    )
    (pi.bin / "curl").chmod(0o755)

    result = pi.run("campaign", "use", "day2", "--write")

    assert result.returncode != 0
    assert pi.local_file.read_text(encoding="utf-8") == original
    assert "did not answer" in result.stderr or "failed to start" in result.stderr


def test_two_switches_at_once_cannot_interleave(pi: Deployment) -> None:
    """The lock is held for the whole stop/write/start/verify sequence, so
    the second operator is told to wait rather than writing the file while
    the first is halfway through."""
    pi.campaign("day2", label="Day two")
    pi.set_local("FIELD_CAMPAIGN=day1\n")
    lock = Path(str(pi.local_file) + ".lock")
    lock.touch()

    held = pi.root / "lock.held"
    holder = subprocess.Popen(
        ["flock", "--exclusive", str(lock), "sh", "-c", f"touch {held}; sleep 30"],
    )
    try:
        # Waited for, rather than assumed: a race here would make this test
        # pass for the wrong reason on a fast machine.
        deadline = time.monotonic() + 10
        while not held.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert held.exists(), "the helper never took the lock"

        result = pi.run("campaign", "use", "day2", "--write")
    finally:
        holder.terminate()
        holder.wait(timeout=10)

    assert result.returncode != 0
    assert "holding" in result.stderr
    assert "FIELD_CAMPAIGN=day1" in pi.local_file.read_text(encoding="utf-8")


def test_no_write_path_ever_puts_the_token_in_its_output(pi: Deployment) -> None:
    pi.campaign("day2", label="Day two")
    pi.set_local("FIELD_CAMPAIGN=day1\n")
    pi.set_service("active")
    pi.set_api_campaign("day2")

    result = pi.run("campaign", "use", "day2", "--write")

    assert TOKEN_VALUE not in result.stdout
    assert TOKEN_VALUE not in result.stderr
    assert TOKEN_VALUE not in pi.local_file.read_text(encoding="utf-8")
    # The token reaches curl through a config file, never as an argument.
    for call in pi.calls("curl"):
        assert TOKEN_VALUE not in call


# -- declaring a campaign ------------------------------------------------


def test_new_copies_the_current_campaign_by_default(pi: Deployment) -> None:
    """A second round is usually the first one on a different day: same
    radio, same band, same capture settings. Declaring it by naming what
    changed beats retyping what did not."""
    pi.set_local("FIELD_CAMPAIGN=day1\n")

    result = pi.run("campaign", "new", "day2", "--label", "Day two", "--write")

    assert result.returncode == 0, result.stderr
    written = (pi.project_root / "campaigns" / "day2.yaml").read_text(encoding="utf-8")
    assert "campaign_id: day2" in written
    assert "label: Day two" in written
    assert "hardware: rsp1a_field" in written
    assert "sample_rate_hz: 768000" in written


def test_new_never_overwrites_an_existing_manifest(pi: Deployment) -> None:
    before = (pi.project_root / "campaigns" / "day1.yaml").read_text(encoding="utf-8")

    result = pi.run("campaign", "new", "day1", "--label", "Something else", "--write")

    assert result.returncode != 0
    assert (pi.project_root / "campaigns" / "day1.yaml").read_text(encoding="utf-8") == before


def test_new_does_not_switch_the_deployment_to_the_campaign_it_declares(
    pi: Deployment,
) -> None:
    """Two deliberate steps, not one. Declaring a round on the laptop in the
    morning must not silently redirect the Pi that is still recording."""
    pi.set_local("FIELD_CAMPAIGN=day1\n")
    pi.set_service("active")

    result = pi.run("campaign", "new", "day2", "--write")

    assert result.returncode == 0, result.stderr
    assert "FIELD_CAMPAIGN=day1" in pi.local_file.read_text(encoding="utf-8")
    assert [call for call in pi.calls("systemctl") if not call.startswith("is-active")] == []


def test_a_campaign_id_that_is_not_a_slug_is_refused(pi: Deployment) -> None:
    """One validator, shared with everything else that reads an id."""
    result = pi.run("campaign", "new", "Day Two", "--write")

    assert result.returncode != 0
    assert not list((pi.project_root / "campaigns").glob("Day*"))


def test_closing_a_campaign_leaves_it_listed_and_readable(pi: Deployment) -> None:
    pi.campaign("day2", label="Day two")
    pi.set_local("FIELD_CAMPAIGN=day2\n")

    closed = pi.run("campaign", "close", "day1", "--write")
    listed = pi.run("campaign", "list")

    assert closed.returncode == 0, closed.stderr
    assert "status: closed" in (pi.project_root / "campaigns" / "day1.yaml").read_text(
        encoding="utf-8"
    )
    assert "# The operator's own note, which nothing here may drop." in (
        pi.project_root / "campaigns" / "day1.yaml"
    ).read_text(encoding="utf-8")
    assert "day1" in listed.stdout
    assert "closed" in listed.stdout


def test_closing_an_already_closed_campaign_is_a_no_op(pi: Deployment) -> None:
    pi.campaign("day2", label="Day two", status="closed")
    pi.set_local("FIELD_CAMPAIGN=day1\n")
    before = (pi.project_root / "campaigns" / "day2.yaml").read_text(encoding="utf-8")

    result = pi.run("campaign", "close", "day2", "--write")

    assert result.returncode == 0, result.stderr
    assert "Already closed" in result.stdout
    assert (pi.project_root / "campaigns" / "day2.yaml").read_text(encoding="utf-8") == before


def test_every_campaign_command_needs_a_project(tmp_path: Path) -> None:
    """Campaigns are declared inside a project. Without one there is no
    manifest to read, and saying so beats a listing of nothing."""
    deployment = Deployment(tmp_path)
    deployment.env_file.write_text(
        deployment.env_file.read_text(encoding="utf-8").replace("FIELD_PROJECT=", "#FIELD_PROJECT="),
        encoding="utf-8",
    )

    result = deployment.run("campaign", "list")

    assert result.returncode != 0
    assert "no project is configured" in result.stderr
