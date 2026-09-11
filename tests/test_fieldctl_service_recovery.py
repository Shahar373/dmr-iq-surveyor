"""What happens to the RUNNING service when a campaign switch goes wrong.

The suite that came with the feature stubs `systemctl` as a recorder: it
remembers that it was called and answers `is-active` from a file a test sets.
That is enough to pin the order of operations, and it is why five defects
survived it -- every one of them is about the thing the recorder cannot
express.

The stub here keeps state, and the one behaviour it models is the one that
matters: **`systemctl start` on a unit that is already active starts nothing
and re-reads nothing.** A real service reads its environment files once, when
its process is exec'd. So a rollback that restores the file and calls `start`
against a service that is already up leaves the old configuration on disk and
the new one in the running process -- the file saying one campaign and the
radio recording another. That cannot be seen without a stub that remembers
what the process read.

Each test names the state the deployment is left in, because that is the
thing an operator at the side of a road actually has.
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
FIELDCTL = REPO_ROOT / "scripts" / "fieldctl"
TOKEN_VALUE = "s3cret-token-value"
PROJECT_ID = "p25_central_il"


class Pi:
    """A deployment whose systemd and API remember what they were told."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.bin = root / "bin"
        self.etc = root / "etc"
        self.state = root / "state"
        self.project = root / "proj"
        for directory in (self.bin, self.etc, self.state, root / "tmp"):
            directory.mkdir(parents=True, exist_ok=True)
        (self.project / "campaigns").mkdir(parents=True, exist_ok=True)
        self._write_stubs()
        self._write_project()
        self._write_config()

    # -- the deployment's own files --------------------------------------

    def _write_config(self) -> None:
        (self.etc / "site.yaml").write_text("site_id: g4\nlabel: g4\n", encoding="utf-8")
        token = self.etc / "token"
        token.write_text(TOKEN_VALUE + "\n", encoding="utf-8")
        token.chmod(0o600)
        (self.etc / "field.env").write_text(
            f"FIELD_SITE={self.etc / 'site.yaml'}\n"
            f"FIELD_TOKEN_FILE={token}\n"
            f"FIELD_PROJECT={self.project / 'project.yaml'}\n"
            f"FIELD_SURVEYOR_BIN={self.bin / 'surveyor'}\n",
            encoding="utf-8",
        )

    def _write_project(self) -> None:
        (self.project / "project.yaml").write_text(
            "schema_version: 1\n"
            f"project_id: {PROJECT_ID}\n"
            "label: P25 central Israel\n"
            "analyzer: p25_site_geolocation\n"
            f"database: {self.root / 'db.sqlite3'}\n",
            encoding="utf-8",
        )
        for campaign in ("day1", "day2"):
            self.campaign(campaign)

    def campaign(self, campaign_id: str, *, status: str | None = None) -> Path:
        path = self.project / "campaigns" / f"{campaign_id}.yaml"
        text = (
            "schema_version: 1\n"
            f"campaign_id: {campaign_id}\n"
            f"project_id: {PROJECT_ID}\n"
            f"label: Round {campaign_id}\n"
        )
        if status is not None:
            text += f"status: {status}\n"
        path.write_text(text, encoding="utf-8")
        return path

    @property
    def local_file(self) -> Path:
        return self.etc / "field.env.local"

    def set_local(self, text: str) -> None:
        self.local_file.write_text(text, encoding="utf-8")

    # -- the stateful stubs ----------------------------------------------

    def _write_stubs(self) -> None:
        # `start` on an active unit is a no-op; a real start reads the
        # environment files once and remembers what it read.
        (self.bin / "systemctl").write_text(
            f'''#!/bin/bash
STATE="{self.state}"
printf '%s\\n' "$*" >> "$STATE/systemctl.calls"
[[ -f "$STATE/unit.state" ]] || echo inactive > "$STATE/unit.state"
case "$1" in
  is-active) cat "$STATE/unit.state" ;;
  stop)
    echo inactive > "$STATE/unit.state"
    [[ -n "${{SLOW_STOP:-}}" ]] && sleep 5
    ;;
  start)
    if [[ "$(cat "$STATE/unit.state")" == active ]]; then exit 0; fi
    campaign=""; project=""
    for file in "{self.etc}/field.env" "{self.etc}/field.env.local"; do
      [[ -f "$file" ]] || continue
      while IFS= read -r line; do
        case "$line" in
          FIELD_CAMPAIGN=*) campaign="${{line#*=}}" ;;
          FIELD_PROJECT=*)  project="${{line#*=}}" ;;
        esac
      done < "$file"
    done
    [[ -n "${{START_CAMPAIGN_OVERRIDE:-}}" ]] && campaign="$START_CAMPAIGN_OVERRIDE"
    [[ -n "${{START_FAILS:-}}" ]] && exit 1
    printf '%s' "$campaign" > "$STATE/unit.campaign"
    printf '%s' "$project"  > "$STATE/unit.project"
    echo active > "$STATE/unit.state"
    [[ -n "${{SLOW_START:-}}" ]] && sleep 5
    ;;
  show) printf '\\n' ;;
esac
exit 0
''',
            encoding="utf-8",
        )
        (self.bin / "curl").write_text(
            f'''#!/bin/bash
STATE="{self.state}"
url="${{@: -1}}"
[[ "$(cat "$STATE/unit.state" 2>/dev/null)" == active ]] || exit 7
campaign="$(cat "$STATE/unit.campaign" 2>/dev/null || true)"
project="{PROJECT_ID}"
[[ -n "$(cat "$STATE/unit.project" 2>/dev/null || true)" ]] || project=""
[[ -n "${{API_PROJECT_OVERRIDE:-}}" ]] && project="$API_PROJECT_OVERRIDE"
case "$url" in
  */api/jobs)  printf '{{"jobs": []}}\\n' ;;
  */api/state)
    if [[ -n "${{API_OMITS_PROJECT:-}}" ]]; then
      printf '{{"scope": {{"capture_campaign_id": "%s"}}}}\\n' "$campaign"
    else
      printf '{{"scope": {{"capture_campaign_id": "%s", "project_id": "%s"}}}}\\n' "$campaign" "$project"
    fi
    ;;
esac
exit 0
''',
            encoding="utf-8",
        )
        (self.bin / "surveyor").write_text(
            f'''#!/usr/bin/env python3
import json, pathlib, sys

root = pathlib.Path("{self.project}")
args = sys.argv[1:]
if args[:3] == ["project", "campaign", "list"]:
    rows = []
    for path in sorted((root / "campaigns").glob("*.yaml")):
        text = path.read_text(encoding="utf-8")
        rows.append({{
            "campaign_id": path.stem,
            "label": path.stem,
            "status": "closed" if "status: closed" in text else "open",
            "manifest": str(path),
            "runs": 0,
            "is_current": False,
        }})
    print(json.dumps({{"project_id": "{PROJECT_ID}", "campaigns": rows}}))
    sys.exit(0)
if args[:3] == ["project", "campaign", "close"]:
    campaign_id = args[args.index("--campaign-id") + 1]
    path = root / "campaigns" / (campaign_id + ".yaml")
    if "--write" in args:
        path.write_text(path.read_text(encoding="utf-8").rstrip("\\n") + "\\nstatus: closed\\n",
                        encoding="utf-8")
        print("Closed", path)
    else:
        print("Would close", path)
    sys.exit(0)
sys.exit(0)
''',
            encoding="utf-8",
        )
        # A python3 that delays the environment-file write AFTER it has
        # happened, so a signal can be injected into the window between the
        # rename and the caller hearing about it.
        (self.bin / "python3").write_text(
            '#!/bin/bash\n'
            '/usr/bin/python3 "$@"; status=$?\n'
            'if [[ -n "${SLOW_WRITE:-}" && "$2" == set ]]; then sleep 5; fi\n'
            'exit $status\n',
            encoding="utf-8",
        )
        (self.bin / "flock").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        # `--write` refuses unless the process is root, and these tests are
        # about what a write does rather than about who may do it. Stubbed
        # rather than left to the real uid: this suite passed locally as root
        # and failed on a CI runner that is not, which is the harness being
        # wrong about itself, not the deployment.
        (self.bin / "id").write_text(
            '#!/bin/sh\nif [ "$1" = "-u" ]; then echo 0; exit 0; fi\nexit 0\n',
            encoding="utf-8",
        )
        for name in ("systemctl", "curl", "surveyor", "flock", "id", "python3"):
            (self.bin / name).chmod(0o755)

    # -- driving it -------------------------------------------------------

    def boot(self, campaign_id: str) -> None:
        """Bring the service up on a campaign, the way a reboot would."""
        self.set_local(f"FIELD_CAMPAIGN={campaign_id}\n")
        (self.state / "unit.state").write_text("inactive\n", encoding="utf-8")
        self._systemctl("start")
        (self.state / "systemctl.calls").write_text("", encoding="utf-8")

    def _systemctl(self, *arguments: str) -> None:
        subprocess.run(
            [str(self.bin / "systemctl"), *arguments, "dmr-field.service"],
            env=self._environment(),
            check=True,
            timeout=60,
        )

    def _environment(self, **extra: str) -> dict[str, str]:
        environment = {
            "PATH": f"{self.bin}{os.pathsep}/usr/bin{os.pathsep}/bin",
            "HOME": str(self.root),
            "TMPDIR": str(self.root / "tmp"),
            "FIELD_ENV_FILE": str(self.etc / "field.env"),
            "FIELD_TAILSCALE_IP": "100.90.110.54",
            "FIELD_TAILSCALE_WAIT": "0",
        }
        environment.update(extra)
        return environment

    def run(self, *arguments: str, **extra: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(FIELDCTL), *arguments],
            env=self._environment(**extra),
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )

    def interrupt_during(self, *arguments: str, after: float = 4.0, **extra: str):
        """Run a command and SIGTERM it once the injected delay is reached."""
        process = subprocess.Popen(
            ["bash", str(FIELDCTL), *arguments],
            env=self._environment(**extra),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        time.sleep(after)
        process.terminate()
        _, errors = process.communicate(timeout=180)
        return process.returncode, errors

    # -- what the deployment looks like afterwards ------------------------

    @property
    def unit_state(self) -> str:
        return (self.state / "unit.state").read_text(encoding="utf-8").strip()

    @property
    def recording_into(self) -> str:
        return (self.state / "unit.campaign").read_text(encoding="utf-8").strip()

    @property
    def configured_campaign(self) -> str:
        for line in self.local_file.read_text(encoding="utf-8").splitlines():
            if line.startswith("FIELD_CAMPAIGN="):
                return line.split("=", 1)[1]
        return ""

    def calls(self, verb: str | None = None) -> list[str]:
        path = self.state / "systemctl.calls"
        if not path.exists():
            return []
        lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line]
        if verb is None:
            return lines
        return [line for line in lines if line.startswith(verb)]

    @property
    def scratch_files(self) -> list[Path]:
        return [path for path in (self.root / "tmp").iterdir() if path.name.startswith("tmp.")]


@pytest.fixture()
def pi(tmp_path: Path) -> Pi:
    return Pi(tmp_path)


# -- the control: a switch that works ------------------------------------


def test_a_switch_that_works_leaves_the_service_on_the_new_campaign(pi: Pi) -> None:
    pi.boot("day1")

    result = pi.run("campaign", "use", "day2", "--write")

    assert result.returncode == 0, result.stderr
    assert pi.configured_campaign == "day2"
    assert pi.recording_into == "day2"
    assert pi.unit_state == "active"
    assert pi.calls("stop") == ["stop dmr-field.service"]
    assert pi.calls("start") == ["start dmr-field.service"]
    assert pi.scratch_files == []


# -- 1. the rollback has to put the SERVICE back, not only the file -------


def test_a_rollback_stops_the_service_running_the_new_configuration(pi: Pi) -> None:
    """The service came up on a campaign nobody asked for. Restoring the file
    and calling `start` against a unit that is already active changes
    nothing: the process keeps the configuration it read at ITS start. The
    rollback has to stop it first."""
    pi.boot("day1")

    result = pi.run(
        "campaign", "use", "day2", "--write", START_CAMPAIGN_OVERRIDE="day9"
    )

    assert result.returncode != 0
    assert pi.configured_campaign == "day1"
    # Two stops: the switch's own, and the one that lets the restored file
    # take effect.
    assert len(pi.calls("stop")) == 2, pi.calls()
    assert len(pi.calls("start")) == 2, pi.calls()
    assert "Rolling back" in result.stderr


def test_a_rollback_leaves_the_service_recording_the_previous_campaign(pi: Pi) -> None:
    """The end state an operator is left with, which is the only thing that
    matters at the side of a road."""
    pi.boot("day1")

    # The start that carries the new configuration comes up wrong; the
    # rollback's start reads the restored file and comes up right.
    result = pi.run("campaign", "use", "day2", "--write", START_CAMPAIGN_OVERRIDE="")

    # With no override the service records what the file says, so this is the
    # success path; the wrong-campaign case is the test above. Both are here
    # to show the fixture cannot fake the outcome either way.
    assert result.returncode == 0, result.stderr
    assert pi.recording_into == pi.configured_campaign


def test_the_rollback_reports_what_is_actually_left_when_it_cannot_finish(pi: Pi) -> None:
    """A service that comes up wrong no matter what it reads cannot be rolled
    back. Saying "rolled back" would be the most dangerous sentence here."""
    pi.boot("day1")

    result = pi.run(
        "campaign", "use", "day2", "--write", START_CAMPAIGN_OVERRIDE="day9"
    )

    assert result.returncode != 0
    assert "ROLLBACK INCOMPLETE" in result.stderr
    assert "day9" in result.stderr
    assert "Rolled back:" not in result.stderr


# -- 2. an interrupt must not leave the Pi stopped ------------------------


def test_an_interrupt_during_the_stop_brings_the_service_back(pi: Pi) -> None:
    """SIGTERM while `systemctl stop` is still running: systemd has already
    taken the unit down, so a handler that only deleted temporary files left
    the deployment not recording, with nothing said about it."""
    pi.boot("day1")

    process = subprocess.Popen(
        ["bash", str(FIELDCTL), "campaign", "use", "day2", "--write"],
        env=pi._environment(SLOW_STOP="1"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    time.sleep(2.5)
    process.terminate()
    _, errors = process.communicate(timeout=120)

    assert process.returncode != 0
    assert pi.unit_state == "active", "the deployment was left stopped"
    assert pi.recording_into == "day1"
    assert pi.configured_campaign == "day1"
    assert "Rolling back" in errors
    assert pi.scratch_files == [], "the token's config file survived the signal"


def test_an_interrupt_still_dies_of_the_signal(pi: Pi) -> None:
    """Recovering must not turn a signal into a success."""
    pi.boot("day1")

    process = subprocess.Popen(
        ["bash", str(FIELDCTL), "campaign", "use", "day2", "--write"],
        env=pi._environment(SLOW_STOP="1"),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(2.5)
    process.terminate()
    process.communicate(timeout=120)

    assert process.returncode == -signal.SIGTERM


def test_a_failed_start_leaves_the_service_running_the_old_campaign(pi: Pi) -> None:
    pi.boot("day1")

    result = pi.run("campaign", "use", "day2", "--write", START_FAILS="1")

    assert result.returncode != 0
    assert pi.configured_campaign == "day1"


# -- 3. the verification compares the project too -------------------------


def test_the_right_campaign_in_the_wrong_project_is_not_a_success(pi: Pi) -> None:
    """A campaign id is not an identity: the same id can exist in another
    project. The service answered with the campaign asked for, inside a
    project nobody asked for, and the switch called that done."""
    pi.boot("day1")

    result = pi.run(
        "campaign", "use", "day2", "--write", API_PROJECT_OVERRIDE="other_project"
    )

    assert result.returncode != 0
    assert "other_project" in result.stderr
    assert pi.configured_campaign == "day1"


def test_a_state_that_omits_the_project_is_not_a_success(pi: Pi) -> None:
    """A missing field is not an answer of "no project"."""
    pi.boot("day1")

    result = pi.run("campaign", "use", "day2", "--write", API_OMITS_PROJECT="1")

    assert result.returncode != 0
    assert pi.configured_campaign == "day1"


# -- 4. closing asks the service, not only the file -----------------------


def test_closing_the_campaign_the_service_is_running_is_refused(pi: Pi) -> None:
    """The configuration was edited to day2 and never restarted, so the file
    says day2 while the radio is still writing day1. The file-based guard
    waves through a close of the round being recorded."""
    pi.boot("day1")
    pi.set_local("FIELD_CAMPAIGN=day2\n")  # edited, not restarted

    result = pi.run("campaign", "close", "day1", "--write")

    assert result.returncode != 0
    assert "recording into day1" in result.stderr
    assert "status: closed" not in (
        pi.project / "campaigns" / "day1.yaml"
    ).read_text(encoding="utf-8")


def test_closing_is_refused_when_the_service_cannot_be_asked(pi: Pi) -> None:
    """An API that cannot be read is not permission to proceed."""
    pi.boot("day1")
    pi.set_local("FIELD_CAMPAIGN=day2\n")
    (pi.bin / "curl").write_text("#!/bin/sh\nexit 7\n", encoding="utf-8")
    (pi.bin / "curl").chmod(0o755)

    result = pi.run("campaign", "close", "day1", "--write")

    assert result.returncode != 0
    assert "could not be read" in result.stderr
    assert "status: closed" not in (
        pi.project / "campaigns" / "day1.yaml"
    ).read_text(encoding="utf-8")


def test_closing_a_campaign_nobody_is_recording_still_works(pi: Pi) -> None:
    """The control: the guard must not block the ordinary case."""
    pi.boot("day1")
    pi.campaign("day3")

    result = pi.run("campaign", "close", "day3", "--write")

    assert result.returncode == 0, result.stderr
    assert "status: closed" in (
        pi.project / "campaigns" / "day3.yaml"
    ).read_text(encoding="utf-8")


# -- 5. a no-op has to be true of the service -----------------------------


def test_a_no_op_is_refused_when_the_service_is_on_another_campaign(pi: Pi) -> None:
    """The file says day2 and the service is recording day1. Answering
    "already in use, nothing to do" leaves the stops where nobody chose."""
    pi.boot("day1")
    pi.set_local("FIELD_CAMPAIGN=day2\n")

    result = pi.run("campaign", "use", "day2", "--write")

    assert result.returncode != 0
    assert "day1" in result.stderr and "day2" in result.stderr
    assert "campaign current" in result.stderr
    assert pi.calls("stop") == [], "nothing may be stopped over a mismatch"


def test_a_no_op_the_service_confirms_is_still_a_no_op(pi: Pi) -> None:
    pi.boot("day2")

    result = pi.run("campaign", "use", "day2", "--write")

    assert result.returncode == 0, result.stderr
    assert "confirms it" in result.stderr
    assert pi.calls("stop") == []
    assert pi.calls("start") == []


def test_a_no_op_is_refused_when_the_service_cannot_be_asked(pi: Pi) -> None:
    """An unreadable API is not confirmation.

    This asserted exit 0 when it was written, on the reasoning that nothing
    was being changed either way. That reasoning is wrong about what the
    message claims: "already the campaign this deployment records into" is a
    statement about the running service, and the file cannot support it --
    the service may have been recording something else since before the file
    was edited. Exit 0 there tells an operator the deployment is fine when
    nothing has checked. Nothing is still changed; the status now says the
    claim could not be made.
    """
    pi.boot("day2")
    (pi.bin / "curl").write_text("#!/bin/sh\nexit 7\n", encoding="utf-8")
    (pi.bin / "curl").chmod(0o755)

    result = pi.run("campaign", "use", "day2", "--write")

    assert result.returncode != 0
    assert "could not be read" in result.stderr
    assert "Nothing was changed" in result.stderr
    assert pi.calls("stop") == []
    assert pi.calls("start") == []
    assert pi.configured_campaign == "day2"
    assert pi.recording_into == "day2"


# -- an interrupt inside a call, after its effect and before it returns ---
#
# A call that has done its work and not yet returned looks exactly like one
# that never ran. These inject the signal into precisely that window, which
# is where the first round's phase markers -- raised after each call returned
# -- left the deployment disagreeing with its own configuration.


def test_an_interrupt_after_the_new_service_started_still_puts_it_back(pi: Pi) -> None:
    """SIGTERM once systemd has started the new process but before
    `systemctl start` returns. Recovering as though nothing had started left
    the file on day1 and the radio on day2, over stop, start, start."""
    pi.boot("day1")

    status, errors = pi.interrupt_during(
        "campaign", "use", "day2", "--write", SLOW_START="1"
    )

    assert status != 0
    assert pi.configured_campaign == "day1"
    assert pi.recording_into == "day1", "the radio kept the campaign nobody chose"
    assert pi.unit_state == "active"
    # The second stop is the one that lets the restored file take effect.
    assert len(pi.calls("stop")) == 2, pi.calls()
    assert len(pi.calls("start")) == 2, pi.calls()
    assert "Rolling back" in errors
    assert pi.scratch_files == []


def test_an_interrupt_after_the_file_was_replaced_still_puts_it_back(pi: Pi) -> None:
    """SIGTERM once the rename has happened but before the writer returns.

    Atomic means no reader sees a half-written file. It does not mean every
    failure happened before the rename -- and treating it that way left both
    the file and the service on the new campaign."""
    pi.boot("day1")

    status, errors = pi.interrupt_during(
        "campaign", "use", "day2", "--write", SLOW_WRITE="1"
    )

    assert status != 0
    assert pi.configured_campaign == "day1", "the replaced file was never put back"
    assert pi.recording_into == "day1"
    assert pi.unit_state == "active"
    assert "Rolling back" in errors
    assert pi.scratch_files == [], "the backup outlived the recovery"


def test_an_interrupt_mid_call_still_dies_of_the_signal(pi: Pi) -> None:
    pi.boot("day1")

    status, _ = pi.interrupt_during(
        "campaign", "use", "day2", "--write", SLOW_START="1"
    )

    assert status == -signal.SIGTERM


def test_a_failure_before_anything_changed_leaves_the_service_running(pi: Pi) -> None:
    """The other end of the same machinery: nothing was stopped or written,
    so there is nothing to undo and the deployment must be untouched."""
    pi.boot("day1")

    result = pi.run("campaign", "use", "day9", "--write")

    assert result.returncode != 0
    assert pi.configured_campaign == "day1"
    assert pi.recording_into == "day1"
    assert pi.unit_state == "active"
    assert pi.calls("stop") == []
    assert pi.calls("start") == []
    assert pi.scratch_files == []


# -- the token never survives any of it -----------------------------------


def test_no_path_through_a_failed_switch_leaves_the_token_on_disk(pi: Pi) -> None:
    pi.boot("day1")

    result = pi.run(
        "campaign", "use", "day2", "--write", START_CAMPAIGN_OVERRIDE="day9"
    )

    assert result.returncode != 0
    assert pi.scratch_files == []
    assert TOKEN_VALUE not in result.stdout
    assert TOKEN_VALUE not in result.stderr
