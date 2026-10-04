"""`cli.fleet_update` against an in-memory cluster (no SSH, shell or launchd) that enforces
production's order: no gateway stop/start while a runner runs, no runner start without it."""

from __future__ import annotations

import argparse
import ast
import json
import plistlib
import re
import shlex
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from cli import fleet_update


def _git(repo: Path, *args: str) -> str:
    command = ["git", "-c", "user.name=t", "-c", "user.email=t@x.invalid", "-C", str(repo), *args]
    return subprocess.run(command, capture_output=True, text=True, check=True).stdout.strip()  # noqa: S603


_IDLE: dict[str, Any] = {
    "dirty": 0,
    "hook": "no",
    "active": "no",
    "hold": ("resumed", None, {}),
    "up": True,
}
_ROW = ("name", "online", "identity_mismatch", "head_sha", "running_sha", "serve_agent_runner")


class Cluster:
    def __init__(self, old: str, new: str) -> None:
        self.new = new
        self.hosts: dict[str, dict[str, Any]] = {
            a: {**_IDLE, "os": o, "head": old}
            for a, o in (("gw", "Linux"), ("mac", "Darwin"), ("lin", "Linux"))
        }
        self.names = {alias: alias for alias in self.hosts}  # alias -> name the host reports
        self.silent: set[str] = set()  # aliases whose heartbeat never reaches the roster
        self.hidden: set[str] = set()  # aliases the roster has no row for
        self.smoked: list[str] = []  # machine names the gateway smoke-tested
        self.laptop: dict[str, Any] | None = None  # a roster-only machine no alias reaches
        self.effects: list[tuple[str, str]] = []
        self.fail: dict[tuple[str, str], int] = {}
        self.stop_failures: dict[str, str] = {}
        self.output = ""
        self.stored_schedules: tuple[int, list[str]] = (
            0,
            [],
        )  # (VERIFY_RC, lines) of NEW's verify on the gateway's table
        self.roster_lag = 0  # roster reads that still show every machine offline (heartbeat)
        self.roster_reads = 0
        self.now = 0.0  # a fake clock: `fleet_update.time` is patched to this object
        self.silence_open = False  # the gateway Grafana's deploy-window silence
        self.silence_hours = 0.0
        self.silence_comment = ""
        self.silence_reply: str | None = None  # a canned `SILENCE ...` line instead of success

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds

    def _runners_up(self) -> bool:
        return any(h["up"] for a, h in self.hosts.items() if a != "gw")

    def _effect(self, kind: str, alias: str, host: dict[str, Any], command: str) -> int:
        self.effects.append((kind, alias))
        if self.fail.get((kind, alias)):
            return self.fail[(kind, alias)]
        if kind == "stop":
            assert not (alias == "gw" and self._runners_up()), "gateway stopped before a runner"
            host["up"], host["hold"] = False, ("paused", "stopped", self.stop_failures)
        elif kind == "switch":
            assert not host["up"] and self.new in command
            assert fleet_update._CLEAN in shlex.split(command)[2]
            assert "uv sync --frozen --compile-bytecode" in command
            host["head"] = self.new
        elif kind in ("start", "oneshot"):
            assert (kind == "oneshot") == (host["os"] == "Darwin")
            assert self.hosts["gw"]["up"] if alias != "gw" else not self._runners_up()
            host["up"], host["hold"] = True, ("resumed", None, {})
        elif kind == "smoke":
            self.smoked.append(command.partition(" - smoke ")[2].split()[0].strip("'"))
        elif kind == "refresh":
            assert "--force" not in command  # human-only
        return 0

    def _roster_json(self) -> str:
        online = self.roster_lag <= 0
        self.roster_reads, self.roster_lag = self.roster_reads + 1, self.roster_lag - 1
        values = [
            (
                self.names[a],
                h["up"] and online and a not in self.silent,
                False,
                h["head"],
                h["head"],
                True,
            )
            for a, h in self.hosts.items()
            if a not in self.hidden
        ]
        if self.laptop:
            values.append(("laptop", *self.laptop["row"]))
        return json.dumps([dict(zip(_ROW, row, strict=True)) for row in values])

    def ssh(self, alias: str, command: str, stdin: str | None, emit: Callable[[str], None]) -> int:
        host = self.hosts[alias]
        emit(self.output)
        if command == "uname -s":
            emit(str(host["os"]))
            return 0
        status, phase, failures = host["hold"]
        hold = json.dumps(
            {"status": status, "maintenance": phase and {"phase": phase, "failures": failures}}
        )
        if '"head=$(git rev-parse HEAD)"' in command:
            for key in ("head", "dirty", "hook", "active"):
                emit(f"{key}={host[key]}")
            emit(f"hold={hold}")
            return 0
        if command.endswith("maintenance status'"):
            emit(hold)
            return 0
        if "machine_name()" in command:
            emit(self.names[alias])
            return 0
        if " - roster" in command:
            assert stdin == fleet_update._GATEWAY_PROGRAM
            emit(self._roster_json())
            return 0
        if stdin == fleet_update._silence_program():
            return self._silence(alias, command, emit)
        if "verify" in command:
            return self._verify(alias, command, emit)
        kinds = {
            "git fetch": "fetch",
            "stop -y": "stop",
            "--detach": "switch",
            "launchctl bootstrap": "oneshot",
            'ava" start': "start",
            " - smoke": "smoke",
            "packages refresh": "refresh",
        }
        kind = next(k for s, k in kinds.items() if s in command)
        if kind == "refresh":
            emit("  summary: applied 2, conflict 1")
        return self._effect(kind, alias, host, command)

    def _silence(self, alias: str, command: str, emit: Callable[[str], None]) -> int:
        """The gateway-side silence program: `python - open --hours H --comment C` / `python - close`."""
        assert alias == "gw", "the silence is Grafana's: it runs on the gateway host"
        script = shlex.split(command)[2]  # the host's login shell runs it as `bash -lc <script>`
        verb, *rest = shlex.split(script.partition('/bin/python" - ')[2])
        self.effects.append((f"silence-{verb}", alias))
        if self.silence_reply is not None:
            emit(self.silence_reply)
            return 0
        if verb == "open":
            self.silence_hours = float(rest[rest.index("--hours") + 1])
            self.silence_comment = rest[rest.index("--comment") + 1]
            self.silence_open = True
            emit("SILENCE opened id=s1 until=later")
        else:
            self.silence_open = False
            emit("SILENCE closed 1")
        return 0

    def _verify(self, alias: str, command: str, emit: Callable[[str], None]) -> int:
        """The drift checks: NEW's verify before any stop, then `schedules verify` / `plugins verify`."""
        kind = (
            "pre-verify"
            if "pre-update-verify" in command
            else "schedules-verify"
            if "schedules verify" in command
            else "plugins-verify"
        )
        self.effects.append((kind, alias))
        if kind == "pre-verify":
            assert alias == "gw" and self.new in command
            # The home's own checkout reads the table first; the worktree only sees the dump.
            assert command.index("_read_schedule_rows") < command.index("worktree add")
            assert command.index("worktree add") < command.index("rows_file=")
            assert all(h["up"] for h in self.hosts.values()), "checked after a service stopped"
            code, lines = self.stored_schedules
            for line in [*lines, f"VERIFY_RC={code}"]:
                emit(line)
            return 0
        assert kind != "schedules-verify" or "--no-notify" in command  # attended: no alert funnel
        if rc := self.fail.get((kind, alias), 0):
            emit(f"RED the {kind} detail from {alias}")
        return rc


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Cluster, Callable[..., int]]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "--initial-branch=main")
    for message in ("old", "new"):
        (repo / ".python-version").write_text("3.12.12\n")
        (repo / "uv.lock").write_text(message)
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", message)
    cluster = Cluster(_git(repo, "rev-parse", "HEAD~1"), _git(repo, "rev-parse", "HEAD"))
    monkeypatch.setattr(fleet_update, "_REPO", repo)
    monkeypatch.setattr(fleet_update, "ssh", cluster.ssh)
    monkeypatch.setattr(fleet_update, "time", cluster)
    monkeypatch.delenv("AVA_AGENT_ID", raising=False)
    hosts = [
        *shlex.split("--gateway gw --runner mac --runner lin --log-dir"),
        str(tmp_path / "logs"),
    ]

    def run(half: str, *extra: str) -> int:
        new = ["--new", cluster.new] if half == "down" else []
        return fleet_update.main([half, *hosts, *new, *extra])

    return cluster, run


def test_runners_stop_first_and_start_last(env: tuple[Cluster, Callable[..., int]]) -> None:
    cluster, run = env
    assert run("down") == 0
    assert run("up") == 0
    kinds = [kind for kind, _ in cluster.effects if not kind.startswith("silence-")]
    assert [a for k, a in cluster.effects if k == "stop"] == ["mac", "lin", "gw"]
    assert kinds.index("stop") > kinds.index("fetch") and kinds.index("switch") > max(
        i for i, k in enumerate(kinds) if k == "stop"
    )
    assert [a for k, a in cluster.effects if k in ("start", "oneshot")] == ["gw", "mac", "lin"]
    assert kinds[-10:-4] == ["smoke"] * 3 + ["refresh"] * 3
    assert set(kinds[-4:]) == {"schedules-verify", "plugins-verify"}
    assert [a for k, a in cluster.effects if k == "refresh"] == ["gw", "mac", "lin"]


def test_drift_checks_run_after_the_smoke_and_the_refresh(
    env: tuple[Cluster, Callable[..., int]],
) -> None:
    """The gateway's in-store schedule scripts are checked once (the DB lives there); every host's
    plugins are checked; all of it after the smoke and the refresh, so a red never skips them."""
    cluster, run = env
    assert run("down") == 0
    assert run("up") == 0
    assert [a for k, a in cluster.effects if k == "schedules-verify"] == ["gw"]
    assert [a for k, a in cluster.effects if k == "plugins-verify"] == ["gw", "mac", "lin"]
    kinds = [kind for kind, _ in cluster.effects]
    first_verify = min(
        i for i, k in enumerate(kinds) if k in ("schedules-verify", "plugins-verify")
    )
    assert first_verify > max(i for i, k in enumerate(kinds) if k in ("smoke", "refresh"))


@pytest.mark.parametrize(
    ("failing", "detail"),
    [
        (("schedules-verify", "gw"), "RED the schedules-verify detail from gw"),
        (("plugins-verify", "mac"), "RED the plugins-verify detail from mac"),
    ],
)
def test_a_red_drift_check_fails_up_with_its_detail(
    env: tuple[Cluster, Callable[..., int]],
    capsys: pytest.CaptureFixture[str],
    failing: tuple[str, str],
    detail: str,
) -> None:
    cluster, run = env
    assert run("down") == 0
    cluster.fail[failing] = 1
    assert run("up") == 1
    out = capsys.readouterr().out
    assert detail in out and "FAILED: drift check failed" in out
    # The smoke and the refresh still ran: a red only fails `up`, last.
    assert {"smoke", "refresh"} <= {k for k, _ in cluster.effects}
    # Every check ran: a red does not hide the next host's.
    assert [a for k, a in cluster.effects if k == "plugins-verify"] == ["gw", "mac", "lin"]


def test_every_red_drift_check_is_reported_not_only_the_first(
    env: tuple[Cluster, Callable[..., int]], capsys: pytest.CaptureFixture[str]
) -> None:
    cluster, run = env
    assert run("down") == 0
    cluster.fail[("schedules-verify", "gw")] = 1
    cluster.fail[("plugins-verify", "lin")] = 1
    assert run("up") == 1
    failed_line = next(
        line for line in capsys.readouterr().out.splitlines() if "drift check failed" in line
    )
    assert "schedules verify" in failed_line and "plugins verify" in failed_line
    assert "gw:" in failed_line and "lin:" in failed_line


_RED_ROW = (
    "RED id=12 name=hierarchy-worker missing=call-signature:L42 catch_up(): "
    "missing a required argument: 'triggers'"
)


def test_stored_schedules_are_checked_against_new_before_anything_stops(
    env: tuple[Cluster, Callable[..., int]],
) -> None:
    cluster, run = env
    assert run("down") == 0
    kinds = [kind for kind, _ in cluster.effects]
    assert kinds.count("pre-verify") == 1
    assert kinds.index("pre-verify") < kinds.index("stop")


def test_a_red_stored_schedule_refuses_down_before_any_stop(
    env: tuple[Cluster, Callable[..., int]], capsys: pytest.CaptureFixture[str]
) -> None:
    """Tonight's shape: a stored script calling `catch_up()` the old way. The refusal lists the
    row and leaves the cluster running."""
    cluster, run = env
    cluster.stored_schedules = (1, ["RESULT ts=t checked=13 green=12 red=1 rc=1", _RED_ROW])
    assert run("down") == 2
    out = capsys.readouterr().out
    assert _RED_ROW in out and "--allow-red-schedules" in out and "would crash-loop" in out
    assert {k for k, _ in cluster.effects} & {"stop", "switch"} == set()


def test_allow_red_schedules_proceeds_and_still_lists_the_rows(
    env: tuple[Cluster, Callable[..., int]], capsys: pytest.CaptureFixture[str]
) -> None:
    cluster, run = env
    cluster.stored_schedules = (1, [_RED_ROW])
    assert run("down", "--allow-red-schedules") == 0
    out = capsys.readouterr().out
    assert "WARNING: stored schedule scripts red" in out and _RED_ROW in out
    assert [a for k, a in cluster.effects if k == "stop"] == ["mac", "lin", "gw"]


def test_a_check_that_could_not_run_refuses_like_a_red(
    env: tuple[Cluster, Callable[..., int]], capsys: pytest.CaptureFixture[str]
) -> None:
    cluster, run = env
    cluster.stored_schedules = (2, ["TOOL-ERROR OperationalError: connection refused"])
    assert run("down") == 2
    out = capsys.readouterr().out
    assert "unevaluable (check exited 2)" in out and "connection refused" in out
    assert {k for k, _ in cluster.effects} & {"stop", "switch"} == set()


def test_the_dry_run_of_down_reports_a_red_without_changing_anything(
    env: tuple[Cluster, Callable[..., int]], capsys: pytest.CaptureFixture[str]
) -> None:
    cluster, run = env
    cluster.stored_schedules = (1, [_RED_ROW])
    assert run("down", "--dry-run") == 2
    assert _RED_ROW in capsys.readouterr().out
    assert [k for k, _ in cluster.effects] == ["pre-verify"]


def test_macos_start_is_a_gui_one_shot() -> None:
    script = fleet_update.gui_oneshot("mac", 60)
    job = plistlib.loads(
        script.split("<<'AVA_FLEET_PLIST'\n")[1].split("\nAVA_FLEET_PLIST")[0].encode()
    )
    assert 'launchctl bootstrap "gui/$(id -u)"' in script
    assert f'launchctl bootout "gui/$(id -u)/{job["Label"]}"' in script
    assert job["RunAtLoad"] is True and job["KeepAlive"] is False
    assert job["ProgramArguments"][:2] == ["/bin/zsh", "-lc"]
    assert 'AVA_HOME="$H" "$S/.venv/bin/ava" start' in job["ProgramArguments"][2]
    assert shlex.split(f"zsh -lc {shlex.quote(script)}")[2] == script and 'rm -rf "$D"' in script


def test_the_macos_one_shot_start_passes_the_home_and_os_job_gates(
    default_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The update's `up` runs the default home's own `<home>/source` CLI, from a
    run-once LaunchAgent in the gui domain. It must pass the checkout guard and the
    default-home gate of the OS-job registrars that `start`'s converge reaches, and
    it must not itself be a registrar's job: its label is its own, and nothing in
    `cli.fleet_update` imports the application."""
    from base.host.system import cron
    from cli.preflight import require_own_checkout

    script = fleet_update.gui_oneshot("mac", 60)
    job = plistlib.loads(
        script.split("<<'AVA_FLEET_PLIST'\n")[1].split("\nAVA_FLEET_PLIST")[0].encode()
    )
    assert re.fullmatch(r"com\.ava\.fleet-update\.mac\.\d{8}T\d{6}", job["Label"])
    command = job["ProgramArguments"][2]
    assert 'H="$HOME/.ava"; S="$H/source"' in command
    assert 'AVA_HOME="$H" "$S/.venv/bin/ava" start' in command

    # `$H` is the default home and `$S` its own checkout: both gates let `start` through.
    home = default_home / ".ava"
    (home / "source").mkdir(parents=True)
    assert require_own_checkout(["start"], home / "source") is None
    assert cron.owns_os_jobs("autostart")

    imported: set[str] = set()
    for node in ast.walk(ast.parse(Path(fleet_update.__file__).read_text())):
        if isinstance(node, ast.Import):
            imported |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
    assert not {"base", "cli", "gateway", "services", "agent", "ava"} & imported


_STOP_RETRY = 'retry "ava stop -y --timeout 600"'


def test_the_first_failure_stops_the_half(
    env: tuple[Cluster, Callable[..., int]], capsys: pytest.CaptureFixture[str]
) -> None:
    cluster, run = env
    cluster.fail[("stop", "mac")] = 1
    assert run("down") == 1
    assert [k for k, _ in cluster.effects if k not in ("fetch", "pre-verify")] == [
        "silence-open",
        "stop",
    ]
    assert _STOP_RETRY in capsys.readouterr().out


def test_a_stop_that_leaves_failures_fails(
    env: tuple[Cluster, Callable[..., int]], capsys: pytest.CaptureFixture[str]
) -> None:
    cluster, run = env
    cluster.stop_failures = {"5": "wake failed"}
    assert run("down") == 1
    assert [a for k, a in cluster.effects if k == "stop"] == ["mac"]
    out = capsys.readouterr().out
    assert "stop left paused/stopped" in out and _STOP_RETRY in out


def test_the_roster_is_re_read_while_heartbeats_catch_up(
    env: tuple[Cluster, Callable[..., int]],
) -> None:
    cluster, run = env
    assert run("down") == 0
    cluster.roster_lag = 3
    assert run("up") == 0
    assert (cluster.roster_reads, cluster.now) == (4, 3 * fleet_update._POLL_S)


def test_a_roster_that_never_agrees_fails_with_its_last_state(
    env: tuple[Cluster, Callable[..., int]], capsys: pytest.CaptureFixture[str]
) -> None:
    cluster, run = env
    assert run("down") == 0
    cluster.roster_lag = 10**6
    assert run("up", "--roster-timeout", "30") == 1
    assert cluster.roster_reads == 30 // fleet_update._POLL_S + 1
    assert "roster after 30s: offline ['gw', 'mac', 'lin']" in capsys.readouterr().out
    assert {k for k, _ in cluster.effects} & {"smoke", "refresh"} == set()


_LAPTOP_OFF = {"row": (False, False, None, None, True)}  # online, mismatch, head, running, runner


def test_an_unlisted_offline_machine_is_reported_not_required(
    env: tuple[Cluster, Callable[..., int]], capsys: pytest.CaptureFixture[str]
) -> None:
    cluster, run = env
    assert run("down") == 0
    cluster.laptop = _LAPTOP_OFF
    assert run("up") == 0
    out = capsys.readouterr().out
    assert "roster: laptop is not listed, not checked: online=False head=None running=None" in out
    assert cluster.smoked == ["gw", "mac", "lin"]
    assert [a for k, a in cluster.effects if k == "refresh"] == ["gw", "mac", "lin"]
    assert "up complete: every listed machine runs" in out


def test_a_listed_machine_that_is_offline_fails(
    env: tuple[Cluster, Callable[..., int]], capsys: pytest.CaptureFixture[str]
) -> None:
    cluster, run = env
    assert run("down") == 0
    cluster.laptop = _LAPTOP_OFF
    cluster.silent.add("lin")
    assert run("up", "--roster-timeout", "10") == 1
    assert "roster after 10s: offline ['lin']" in capsys.readouterr().out
    assert {k for k, _ in cluster.effects} & {"smoke", "refresh"} == set()


def test_the_roster_name_comes_from_the_host_not_its_ssh_alias(
    env: tuple[Cluster, Callable[..., int]],
) -> None:
    cluster, run = env
    assert run("down") == 0
    cluster.names["mac"] = "mac-mini"
    assert run("up") == 0
    assert cluster.smoked == ["gw", "mac-mini", "lin"]


def test_a_listed_host_with_no_roster_row_is_an_error(
    env: tuple[Cluster, Callable[..., int]], capsys: pytest.CaptureFixture[str]
) -> None:
    cluster, run = env
    assert run("down") == 0
    cluster.hidden.add("lin")
    assert run("up") == 1
    assert "no roster row for ['lin']" in capsys.readouterr().out
    assert {k for k, _ in cluster.effects} & {"smoke", "refresh"} == set()


def test_packages_refresh_reports_and_never_forces(
    env: tuple[Cluster, Callable[..., int]], capsys: pytest.CaptureFixture[str]
) -> None:
    _, run = env
    assert run("down") == 0
    assert run("up") == 0
    out = capsys.readouterr().out
    assert out.count("packages refresh [") == 3
    assert "packages refresh [mac] summary: applied 2, conflict 1" in out


def test_a_dirty_tree_is_shown_not_cleaned(tmp_path: Path) -> None:
    repo = tmp_path / "dirty"
    repo.mkdir()
    _git(repo, "init", "-q", "--initial-branch=main")

    def check() -> subprocess.CompletedProcess[str]:
        argv = ["bash", "-c", fleet_update._CLEAN]
        return subprocess.run(argv, cwd=repo, capture_output=True, text=True, check=False)  # noqa: S603

    assert check().returncode == 0
    for n in range(25):
        (repo / f"stray-{n:02}.txt").write_text("x")
    dirty = check()
    assert dirty.returncode == 1
    assert dirty.stdout.count("?? stray-") == 20 and "?? stray-00.txt" in dirty.stdout
    assert "move them away (do not delete)" in dirty.stdout
    assert len(list(repo.glob("stray-*.txt"))) == 25


_UNSAFE: list[tuple[str, object]] = [("hook", "yes"), ("active", "yes"), ("dirty", 3)]


@pytest.mark.parametrize(("key", "value"), [*_UNSAFE, ("hold", ("paused", "draining", None))])
def test_preflight_refuses(
    env: tuple[Cluster, Callable[..., int]], key: str, value: object
) -> None:
    cluster, run = env
    cluster.hosts["lin"][key] = value
    assert run("down") == 2
    assert cluster.effects == []


def test_refuses_inside_an_ava_agent(
    env: tuple[Cluster, Callable[..., int]], monkeypatch: pytest.MonkeyPatch
) -> None:
    cluster, run = env
    monkeypatch.setenv("AVA_AGENT_ID", "7")
    assert run("down") == 2
    assert cluster.effects == []


def test_a_python_change_needs_the_flag(env: tuple[Cluster, Callable[..., int]]) -> None:
    cluster, run = env
    repo = fleet_update._REPO
    (repo / ".python-version").write_text("3.12.13\n")
    _git(repo, "commit", "-qam", "python")
    cluster.new = _git(repo, "rev-parse", "HEAD")
    assert run("down") == 2 and cluster.effects == []
    assert run("down", "--allow-python-change") == 0


def test_dry_run_changes_nothing(env: tuple[Cluster, Callable[..., int]]) -> None:
    cluster, run = env
    assert run("down", "--dry-run") == 0
    assert run("up", "--dry-run") == 0
    # Only the read-only stored-schedule check runs (a throwaway worktree, no service touched).
    assert [k for k, _ in cluster.effects] == ["pre-verify"]


def test_the_log_carries_no_secret(
    env: tuple[Cluster, Callable[..., int]], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cluster, run = env
    cluster.output = "AVA_CLUSTER_SECRET=s3cret-a Bearer tok-b postgresql://u:pw-c@h/db"
    assert run("down") == 0
    logs = list((tmp_path / "logs").iterdir())
    written = "".join(path.read_text() for path in logs) + capsys.readouterr().out
    assert "<redacted>" in written
    assert not [secret for secret in ("s3cret-a", "tok-b", "pw-c") if secret in written]
    assert all(path.stat().st_mode & 0o077 == 0 for path in logs)


_HOLD_WITH_BACKSLASHES = (
    '{"status": "paused", "maintenance": {"phase": "stopped", '
    '"failures": {"argv": "[\\"sh\\", \\"-c\\", \\"\\\\$HOME\\\\n\\"]"}}}'
)


@pytest.mark.parametrize("shell", ["/bin/zsh", "/bin/sh"])
def test_the_probe_carries_backslashes_in_the_hold_record_unchanged(
    shell: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """zsh's and dash's `echo` interpret backslashes: a hold record whose argv holds `\\$HOME`
    reached `_hold` as an invalid escape. The probe runs in each host's real shell."""
    if not Path(shell).exists():
        pytest.skip(f"{shell} is not installed")
    source = tmp_path / ".ava" / "source"
    (source / ".venv" / "bin").mkdir(parents=True)
    _git(source, "init", "-q", "--initial-branch=main")
    _git(source, "commit", "-q", "--allow-empty", "-m", "base")
    (source / ".git" / "info" / "exclude").write_text(".venv/\n")
    fake = source / ".venv" / "bin" / "ava"
    fake.write_text(f"#!/bin/sh\ncat <<'EOF'\nnoise line\n{_HOLD_WITH_BACKSLASHES}\nEOF\n")
    fake.chmod(0o755)
    monkeypatch.setenv("HOME", str(tmp_path))

    result = subprocess.run(  # noqa: S603
        [shell, "-c", fleet_update._PROBE], capture_output=True, text=True, check=True
    )

    facts = {
        key: value for key, _, value in (line.partition("=") for line in result.stdout.splitlines())
    }
    assert facts["head"] == _git(source, "rev-parse", "HEAD")
    assert facts["dirty"] == "0"
    assert fleet_update._hold(facts["hold"]) == (
        "paused",
        "stopped",
        {"argv": '["sh", "-c", "\\$HOME\\n"]'},
    )


@pytest.mark.parametrize("shell", ["/bin/zsh", "/bin/sh"])
def test_the_machine_name_is_asked_through_the_hosts_real_shell(
    shell: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The roster program ends in `python -` (the script arrives on stdin); a `-c` question
    appended to it ran `python - -c ...`, which read an empty stdin and printed nothing, so
    `up` died on an empty answer. The command string runs here in a real shell, against a
    `python` that behaves as the host's does."""
    if not Path(shell).exists():
        pytest.skip(f"{shell} is not installed")
    source = tmp_path / ".ava" / "source"
    (source / "base" / "cluster").mkdir(parents=True)
    for package in (source / "base", source / "base" / "cluster"):
        (package / "__init__.py").write_text("")
    (source / "base" / "cluster" / "machine.py").write_text(
        'def machine_name() -> str:\n    return "mac-mini"\n'
    )
    python = source / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
    python.chmod(0o755)
    monkeypatch.setenv("HOME", str(tmp_path))

    def ssh(_alias: str, command: str, stdin: str | None, emit: Callable[[str], None]) -> int:
        return _run_locally(shell, command, stdin, emit)

    monkeypatch.setattr(fleet_update, "ssh", ssh)
    args = argparse.Namespace(dry_run=False, log_dir=tmp_path / "logs", half="up")

    names = fleet_update._machine_names(fleet_update.Session(args), ["mac"])

    assert names == {"mac": "mac-mini"}


def _run_locally(shell: str, command: str, stdin: str | None, emit: Callable[[str], None]) -> int:
    done = subprocess.run(  # noqa: S603
        [shell, "-c", command], input=stdin or "", capture_output=True, text=True, check=False
    )
    for line in done.stdout.splitlines():
        emit(line)
    return done.returncode


def test_the_window_is_one_silence_opened_before_the_first_stop_and_closed_after_up(
    env: tuple[Cluster, Callable[..., int]],
) -> None:
    cluster, run = env
    assert run("down", "--silence-hours", "3") == 0
    kinds = [kind for kind, _ in cluster.effects]
    assert kinds.index("silence-open") < kinds.index("stop")
    assert cluster.silence_open and cluster.silence_hours == 3.0
    assert cluster.new[:12] in cluster.silence_comment
    assert run("up") == 0
    assert not cluster.silence_open
    assert [k for k, _ in cluster.effects if k.startswith("silence-")] == [
        "silence-open",
        "silence-close",
    ]
    assert cluster.effects[-1] == ("silence-close", "gw")


def test_a_failed_up_leaves_the_silence_to_its_expiry(
    env: tuple[Cluster, Callable[..., int]],
) -> None:
    cluster, run = env
    assert run("down") == 0
    cluster.fail[("schedules-verify", "gw")] = 1
    assert run("up") == 1
    assert cluster.silence_open
    assert "silence-close" not in {k for k, _ in cluster.effects}


@pytest.mark.parametrize(
    "reply",
    [
        "SILENCE failed: GET /api/x: URLError",
        "SILENCE skipped: no Grafana admin credential is configured",
    ],
)
def test_a_silence_that_cannot_open_warns_and_the_update_goes_on(
    env: tuple[Cluster, Callable[..., int]], capsys: pytest.CaptureFixture[str], reply: str
) -> None:
    cluster, run = env
    cluster.silence_reply = reply
    assert run("down") == 0
    assert (
        f"WARNING: alert silence not open: {reply.removeprefix('SILENCE ')}"
        in capsys.readouterr().out
    )
    assert [a for k, a in cluster.effects if k == "stop"] == ["mac", "lin", "gw"]


def test_the_dry_run_lists_the_silence_without_opening_it(
    env: tuple[Cluster, Callable[..., int]], capsys: pytest.CaptureFixture[str]
) -> None:
    cluster, run = env
    assert run("down", "--dry-run") == 0
    assert " - open --hours 4 --comment " in capsys.readouterr().out
    assert not cluster.silence_open
    assert [k for k, _ in cluster.effects if k.startswith("silence-")] == []
