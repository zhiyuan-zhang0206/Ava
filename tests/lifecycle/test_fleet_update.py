"""`cli.fleet_update` against an in-memory cluster (no SSH, shell or launchd) that enforces
production's order: no gateway stop/start while a runner runs, no runner start without it."""

from __future__ import annotations

import json
import plistlib
import shlex
import subprocess
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
        self.effects: list[tuple[str, str]] = []
        self.fail: dict[tuple[str, str], int] = {}
        self.stop_failures: dict[str, str] = {}
        self.output = ""

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
            host["head"] = self.new
        elif kind in ("start", "oneshot"):
            assert (kind == "oneshot") == (host["os"] == "Darwin")
            assert self.hosts["gw"]["up"] if alias != "gw" else not self._runners_up()
            host["up"], host["hold"] = True, ("resumed", None, {})
        return 0

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
        if 'echo "head=' in command:
            for key in ("head", "dirty", "hook", "active"):
                emit(f"{key}={host[key]}")
            emit(f"hold={hold}")
            return 0
        if command.endswith("maintenance status'"):
            emit(hold)
            return 0
        if " - roster" in command:
            assert stdin == fleet_update._GATEWAY_PROGRAM
            values = [
                (a, h["up"], False, h["head"], h["head"], True) for a, h in self.hosts.items()
            ]
            emit(json.dumps([dict(zip(_ROW, row, strict=True)) for row in values]))
            return 0
        kinds = {
            "git fetch": "fetch",
            "stop -y": "stop",
            "--detach": "switch",
            "launchctl bootstrap": "oneshot",
            'ava" start': "start",
            " - smoke": "smoke",
        }
        return self._effect(next(k for s, k in kinds.items() if s in command), alias, host, command)


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
    kinds = [kind for kind, _ in cluster.effects]
    assert [a for k, a in cluster.effects if k == "stop"] == ["mac", "lin", "gw"]
    assert kinds.index("stop") > kinds.index("fetch") and kinds.index("switch") > max(
        i for i, k in enumerate(kinds) if k == "stop"
    )
    assert [a for k, a in cluster.effects if k in ("start", "oneshot")] == ["gw", "mac", "lin"]
    assert kinds[-3:] == ["smoke"] * 3


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


def test_the_first_failure_stops_the_half(env: tuple[Cluster, Callable[..., int]]) -> None:
    cluster, run = env
    cluster.fail[("stop", "mac")] = 1
    assert run("down") == 1
    assert [k for k, _ in cluster.effects if k != "fetch"] == ["stop"]


def test_a_stop_that_leaves_failures_fails(env: tuple[Cluster, Callable[..., int]]) -> None:
    cluster, run = env
    cluster.stop_failures = {"5": "wake failed"}
    assert run("down") == 1
    assert [a for k, a in cluster.effects if k == "stop"] == ["mac"]


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
    assert cluster.effects == []


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
