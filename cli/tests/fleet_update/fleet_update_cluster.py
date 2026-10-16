from __future__ import annotations

import json
import shlex
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from cli import fleet_update


def git(repo: Path, *args: str) -> str:
    command = ["git", "-c", "user.name=t", "-c", "user.email=t@x.invalid", "-C", str(repo), *args]
    return subprocess.run(command, capture_output=True, text=True, check=True).stdout.strip()


_IDLE: dict[str, Any] = {
    "dirty": 0,
    "hook": "no",
    "active": "no",
    "hold": ("resumed", None),
    "up": True,
}
_ROW = ("name", "online", "identity_mismatch", "head_sha", "running_sha", "serve_agent_runner")


# ava:ref cli/tests/fleet_update/docs/fleet-update-harness.ava.okf.md#harness-order-guards
class Cluster:
    def __init__(self, old: str, new: str) -> None:
        self.new = new
        self.hosts: dict[str, dict[str, Any]] = {
            a: {**_IDLE, "os": o, "head": old}
            for a, o in (("gw", "Linux"), ("mac", "Darwin"), ("lin", "Linux"))
        }
        self.names = {alias: alias for alias in self.hosts}
        self.silent: set[str] = set()
        self.hidden: set[str] = set()
        self.smoked: list[str] = []
        self.laptop: dict[str, Any] | None = None
        self.effects: list[tuple[str, str]] = []
        self.commands: list[tuple[str, str]] = []
        self.run_started_at = 0
        self.fail: dict[tuple[str, str], int] = {}
        self.output = ""
        self.stored_schedules: tuple[int, list[str]] = (0, [])
        self.roster_lag = 0
        self.roster_reads = 0
        self.now = 0.0
        self.silence_open = False
        self.silence_hours = 0.0
        self.silence_comment = ""
        self.silence_reply: str | None = None

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
            host["up"], host["hold"] = False, ("paused", "stopped")
        elif kind == "switch":
            assert not host["up"] and self.new in command
            assert fleet_update.CLEAN in shlex.split(command)[2]
            assert "uv sync --frozen --compile-bytecode" in command
            host["head"] = self.new
        elif kind in ("start", "oneshot"):
            assert (kind == "oneshot") == (host["os"] == "Darwin")
            assert self.hosts["gw"]["up"] if alias != "gw" else not self._runners_up()
            host["up"], host["hold"] = True, ("resumed", None)
        elif kind == "smoke":
            self.smoked.append(command.partition(" - smoke ")[2].split()[0].strip("'"))
        elif kind == "refresh":
            assert "--force" not in command
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
        self.commands.append((alias, command))
        host = self.hosts[alias]
        emit(self.output)
        if command == "uname -s":
            emit(str(host["os"]))
            return 0
        status, phase = host["hold"]
        hold = json.dumps(
            {
                "hold": {
                    "status": status,
                    "maintenance": phase and {"phase": phase},
                }
            }
        )
        if '"head=$(git rev-parse HEAD)"' in command:
            for key in ("head", "dirty", "hook", "active"):
                emit(f"{key}={host[key]}")
            emit(f"hold={hold}")
            return 0
        if command.endswith("status --json'"):
            emit(hold)
            return 0
        if "machine_from_boot(ConfigBoot()).name()" in command:
            emit(self.names[alias])
            return 0
        if " - roster" in command:
            assert stdin == fleet_update.gateway_program()
            emit(self._roster_json())
            return 0
        if stdin == fleet_update.silence_program():
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
        assert alias == "gw", "the silence is Grafana's: it runs on the gateway host"
        script = shlex.split(command)[2]
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
            assert command.index("_read_schedule_rows") < command.index("worktree add")
            assert command.index("worktree add") < command.index("cli.main")
            fresh = [k for k, _ in self.effects[self.run_started_at :]]
            assert "stop" not in fresh, "checked after a service stopped"
            code, lines = self.stored_schedules
            for line in [*lines, f"VERIFY_RC={code}"]:
                emit(line)
            return 0
        assert kind != "schedules-verify" or "--no-notify" in command
        if rc := self.fail.get((kind, alias), 0):
            emit(f"RED the {kind} detail from {alias}")
        return rc


# ava:ref cli/tests/fleet_update/docs/fleet-update-harness.ava.okf.md#keep-decision-stub
def stub_decision(repo: Path, payload: dict[str, Any]) -> Path:
    path = repo / "scripts" / "host_ops" / "fleet_update_keep.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "import json, sys\n"
        f"print(json.dumps({payload!r}))\n"
        "open(__file__ + '.argv', 'a').write(' '.join(sys.argv) + chr(10))\n"
    )
    return path


def decision_argvs(stub: Path) -> list[str]:
    return Path(str(stub) + ".argv").read_text().splitlines()
