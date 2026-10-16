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

import pytest

from cli import fleet_update
from cli.tests.fleet_update.fleet_update_cluster import Cluster, git


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
    assert {"smoke", "refresh"} <= {k for k, _ in cluster.effects}
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


def test_the_roster_is_re_read_while_heartbeats_catch_up(
    env: tuple[Cluster, Callable[..., int]],
) -> None:
    cluster, run = env
    assert run("down") == 0
    cluster.roster_lag = 3
    assert run("up") == 0
    assert (cluster.roster_reads, cluster.now) == (4, 3 * fleet_update.POLL_S)


def test_a_roster_that_never_agrees_fails_with_its_last_state(
    env: tuple[Cluster, Callable[..., int]], capsys: pytest.CaptureFixture[str]
) -> None:
    cluster, run = env
    assert run("down") == 0
    cluster.roster_lag = 10**6
    assert run("up", "--roster-timeout", "30") == 1
    assert cluster.roster_reads == 30 // fleet_update.POLL_S + 1
    assert "roster after 30s: offline ['gw', 'mac', 'lin']" in capsys.readouterr().out
    assert {k for k, _ in cluster.effects} & {"smoke", "refresh"} == set()


_LAPTOP_OFF = {"row": (False, False, None, None, True)}


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
    git(repo, "init", "-q", "--initial-branch=main")

    def check() -> subprocess.CompletedProcess[str]:
        argv = ["bash", "-c", fleet_update.CLEAN]
        return subprocess.run(argv, cwd=repo, capture_output=True, text=True, check=False)

    assert check().returncode == 0
    for n in range(25):
        (repo / f"stray-{n:02}.txt").write_text("x")
    dirty = check()
    assert dirty.returncode == 1
    assert dirty.stdout.count("?? stray-") == 20 and "?? stray-00.txt" in dirty.stdout
    assert "move them away (do not delete)" in dirty.stdout
    assert len(list(repo.glob("stray-*.txt"))) == 25


_UNSAFE: list[tuple[str, object]] = [("hook", "yes"), ("active", "yes"), ("dirty", 3)]


@pytest.mark.parametrize(("key", "value"), [*_UNSAFE, ("hold", ("paused", "draining"))])
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


def test_hosts_that_disagree_on_head_are_refused(
    env: tuple[Cluster, Callable[..., int]], capsys: pytest.CaptureFixture[str]
) -> None:
    cluster, run = env
    cluster.hosts["lin"]["head"] = "deadbeefdeadbeef"
    assert run("down") == 2
    assert cluster.effects == []
    assert "hosts disagree on HEAD" in capsys.readouterr().out


def test_a_python_change_needs_the_flag(env: tuple[Cluster, Callable[..., int]]) -> None:
    cluster, run = env
    repo = fleet_update.REPO
    (repo / ".python-version").write_text("3.12.13\n")
    git(repo, "commit", "-qam", "python")
    cluster.new = git(repo, "rev-parse", "HEAD")
    assert run("down") == 2 and cluster.effects == []
    assert run("down", "--allow-python-change") == 0


def test_dry_run_changes_nothing(env: tuple[Cluster, Callable[..., int]]) -> None:
    cluster, run = env
    assert run("down", "--dry-run") == 0
    assert run("up", "--dry-run") == 0
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
    '{"hold": {"status": "paused", "operation": "[\\"sh\\", \\"-c\\", \\"\\\\$HOME\\\\n\\"]", '
    '"maintenance": {"phase": "stopped"}}}'
)


@pytest.mark.parametrize("shell", ["/bin/zsh", "/bin/sh"])
def test_the_probe_carries_backslashes_in_the_hold_record_unchanged(
    shell: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if not Path(shell).exists():
        pytest.skip(f"{shell} is not installed")
    source = tmp_path / ".ava" / "source"
    (source / ".venv" / "bin").mkdir(parents=True)
    git(source, "init", "-q", "--initial-branch=main")
    git(source, "commit", "-q", "--allow-empty", "-m", "base")
    (source / ".git" / "info" / "exclude").write_text(".venv/\n")
    fake = source / ".venv" / "bin" / "ava"
    fake.write_text(f"#!/bin/sh\ncat <<'EOF'\nnoise line\n{_HOLD_WITH_BACKSLASHES}\nEOF\n")
    fake.chmod(0o755)
    monkeypatch.setenv("HOME", str(tmp_path))

    result = subprocess.run(
        [shell, "-c", fleet_update.PROBE], capture_output=True, text=True, check=True
    )

    facts = {
        key: value for key, _, value in (line.partition("=") for line in result.stdout.splitlines())
    }
    assert facts["head"] == git(source, "rev-parse", "HEAD")
    assert facts["dirty"] == "0"
    assert fleet_update.split_hold_text(facts["hold"]) == ("paused", "stopped")
    assert json.loads(facts["hold"])["hold"]["operation"] == '["sh", "-c", "\\$HOME\\n"]'


@pytest.mark.parametrize("shell", ["/bin/zsh", "/bin/sh"])
def test_the_machine_name_is_asked_through_the_hosts_real_shell(
    shell: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if not Path(shell).exists():
        pytest.skip(f"{shell} is not installed")
    source = tmp_path / ".ava" / "source"
    (source / "base" / "cluster" / "machine").mkdir(parents=True)
    for package in (
        source / "base",
        source / "base" / "cluster",
        source / "base" / "cluster" / "machine",
    ):
        (package / "__init__.py").write_text("")
    (source / "base" / "cluster" / "machine" / "boot.py").write_text(
        'class Identity:\n    def name(self) -> str:\n        return "mac-mini"\n'
        "def machine_from_boot(config) -> Identity:\n    return Identity()\n"
    )
    (source / "base" / "config.py").write_text("class ConfigBoot: pass\n")
    python = source / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
    python.chmod(0o755)
    monkeypatch.setenv("HOME", str(tmp_path))

    def ssh(_alias: str, command: str, stdin: str | None, emit: Callable[[str], None]) -> int:
        return _run_locally(shell, command, stdin, emit)

    monkeypatch.setattr(fleet_update, "ssh", ssh)
    args = argparse.Namespace(dry_run=False, log_dir=tmp_path / "logs", half="up")

    names = fleet_update.machine_names(fleet_update.Session(args), ["mac"])

    assert names == {"mac": "mac-mini"}


def _run_locally(shell: str, command: str, stdin: str | None, emit: Callable[[str], None]) -> int:
    done = subprocess.run(
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
