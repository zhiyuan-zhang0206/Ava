"""In-container observer for the Linux verification container.

Runs inside the container after `ava start`, from the checkout's own interpreter:

    cd ~/.ava/source && PYTHONPATH=. .venv/bin/python observe.py OUT.json

It checks the started cluster from the outside, the way an operator would, and
writes one JSON document with every check's outcome. A failed check is recorded and
the remaining checks still run, so a partial result keeps its evidence; the exit
status is nonzero when any check failed.

- toolchain: the installed data-plane and runtime versions, Redis on the approved
  8.2 series, pgvector present;
- source: the checkout is at the commit the run named and has no changed file;
- services: every service of the verification profile answers its identity probe;
- frontend: the app port answers over HTTP;
- cors: the gateway answers the frontend's exact browser origin, credentialed;
- scripted_agent: an agent driven by the scripted model has executed
  `print(1 + 2)` through the real gateway and agent host, and the recorded output
  body is `3` (not a model claim, not a digit in a timestamp).
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

SERVICES = frozenset({"gateway", "frontend", "ops", "agent-host"})
PG_BIN = Path("/usr/lib/postgresql/17/bin")
AGENT_DEADLINE_S = 120
# The timeline items kept as evidence; the system prompt alone is tens of kilobytes.
EVIDENCE_KINDS = frozenset({"agent_reasoning", "agent_code", "code_output", "agent_chat"})

Detail = dict[str, Any]


def _out(argv: list[str]) -> str:
    return subprocess.run(  # noqa: S603 — fixed read-only commands, no shell
        argv, capture_output=True, text=True, timeout=20, check=True
    ).stdout.strip()


def _ports(home: Path) -> dict[str, int]:
    record = json.loads((home / "start-intent.json").read_text())["record"]
    return record["ports"]


def check_toolchain(_home: Path, _source: Path) -> Detail:
    versions = {
        "arch": _out(["uname", "-m"]),
        "python": _out([sys.executable, "--version"]),
        "uv": _out(["uv", "--version"]),
        "node": _out(["node", "--version"]),
        "postgres": _out([str(PG_BIN / "postgres"), "--version"]),
        "redis": _out(["redis-server", "--version"]).split(" sha=")[0],
        "pgbouncer": _out(["pgbouncer", "--version"]).splitlines()[0],
    }
    if not re.search(r"\bv=8\.2\.\d+$", versions["redis"]):
        raise RuntimeError(f"Redis is not on the approved 8.2 series: {versions['redis']}")
    if "(PostgreSQL) 17." not in versions["postgres"]:
        raise RuntimeError(f"PostgreSQL is not 17: {versions['postgres']}")
    if not Path("/usr/share/postgresql/17/extension/vector.control").is_file():
        raise RuntimeError("pgvector is not installed for PostgreSQL 17")
    return {"versions": versions, "pgvector": "installed"}


def check_source(_home: Path, source: Path) -> Detail:
    commit = _out(["git", "-C", str(source), "rev-parse", "HEAD"])
    changed = _out(["git", "-C", str(source), "status", "--porcelain"])
    if changed:
        raise RuntimeError(f"the checkout has changed or untracked files:\n{changed}")
    return {"commit": commit, "changed_files": []}


def check_services(_home: Path, _source: Path) -> Detail:
    from ops.roster import build_services

    specs = [spec for spec in build_services() if spec.session in SERVICES]
    if frozenset(spec.session for spec in specs) != SERVICES:
        raise RuntimeError("the checkout does not implement the verification service roster")
    rows: dict[str, Detail] = {}
    for spec in specs:
        if spec.identity_probe is None:
            raise RuntimeError(f"service {spec.session} has no identity probe")
        probe = spec.identity_probe()
        rows[spec.session] = {"verdict": probe.verdict.name, "detail": probe.detail}
    down = sorted(name for name, row in rows.items() if row["verdict"] != "ALIVE")
    if down:
        raise RuntimeError(f"services not alive after a successful start: {down}\n{rows}")
    return {"services": rows}


def check_frontend(home: Path, _source: Path) -> Detail:
    url = f"http://127.0.0.1:{_ports(home)['app']}"
    response = httpx.get(url, timeout=10)
    response.raise_for_status()
    return {"url": url, "status": response.status_code}


def check_cors(home: Path, _source: Path) -> Detail:
    ports = _ports(home)
    origin = f"http://127.0.0.1:{ports['app']}"
    response = httpx.get(
        f"http://127.0.0.1:{ports['gateway']}/api/auth/check",
        headers={"Origin": origin},
        timeout=10,
    )
    response.raise_for_status()
    seen = {
        "allow_origin": response.headers.get("access-control-allow-origin"),
        "allow_credentials": response.headers.get("access-control-allow-credentials"),
        "authenticated": response.json()["authenticated"],
    }
    if seen != {"allow_origin": origin, "allow_credentials": "true", "authenticated": True}:
        raise RuntimeError(f"the gateway does not answer the frontend origin {origin}: {seen}")
    return {"origin": origin, **seen}


def execution_completed(items: list[dict[str, Any]], reply: str) -> bool:
    """Judge the executed body, never a digit in its timestamp or a model claim."""
    code = [i["payload"].strip() for i in items if i["kind"] == "agent_code"]
    replies = [i["payload"] for i in items if i["kind"] == "agent_chat"]
    outputs = [i for i in items if i["kind"] == "code_output"]
    if code != ["print(1 + 2)"] or reply not in replies or len(outputs) != 1:
        return False
    header, separator, body = outputs[0]["payload"].partition("\n\n")
    return bool(
        separator
        and body.strip() == "3"
        and outputs[0]["exec_ms"] is not None
        and "cancelled" not in header
        and "timeout" not in header
    )


def check_scripted_agent(home: Path, _source: Path) -> Detail:
    from tests.e2e.fakes.scenarios.message_flow import REPLY_TEXT

    with httpx.Client(base_url=f"http://127.0.0.1:{_ports(home)['gateway']}", timeout=30) as client:
        created = client.post(
            "/api/agents",
            json={"spawner": "user", "prompt": "Compute 1+2.", "prompt_source": "user"},
        )
        created.raise_for_status()
        agent = created.json()["id"]
        deadline = time.monotonic() + AGENT_DEADLINE_S
        items: list[dict[str, Any]] = []
        while time.monotonic() < deadline:
            timeline = client.get(f"/api/agents/{agent}/timeline?limit=1000")
            timeline.raise_for_status()
            items = timeline.json()["items"]
            if execution_completed(items, REPLY_TEXT):
                return {
                    "agent": agent,
                    "model": "scripted message_flow",
                    "timeline": [
                        {"kind": i["kind"], "payload": i["payload"], "exec_ms": i["exec_ms"]}
                        for i in items
                        if i["kind"] in EVIDENCE_KINDS
                    ],
                }
            time.sleep(1)
    raise RuntimeError(f"agent {agent} did not complete scripted execution: {items}")


CHECKS: dict[str, Callable[[Path, Path], Detail]] = {
    "toolchain": check_toolchain,
    "source": check_source,
    "services": check_services,
    "frontend": check_frontend,
    "cors": check_cors,
    "scripted_agent": check_scripted_agent,
}


def _cgroup(name: str) -> str | None:
    """A cgroup v2 value of this container (memory.peak, memory.max), None when absent."""
    path = Path("/sys/fs/cgroup") / name
    return path.read_text().strip() if path.is_file() else None


def observe(home: Path, source: Path) -> Detail:
    """Run every check, recording each outcome; never stop at the first failure."""
    checks: dict[str, Detail] = {}
    for name, check in CHECKS.items():
        started = time.monotonic()
        try:
            checks[name] = {"ok": True, "detail": check(home, source)}
        except Exception as error:
            checks[name] = {"ok": False, "error": f"{type(error).__name__}: {error}"}
        checks[name]["seconds"] = round(time.monotonic() - started, 3)
    return {
        "at": time.time(),
        "result": "passed" if all(row["ok"] for row in checks.values()) else "failed",
        # What the whole run needed (dependency installs, the frontend build, the cluster).
        "memory": {"peak_bytes": _cgroup("memory.peak"), "limit_bytes": _cgroup("memory.max")},
        "checks": checks,
    }


def main() -> int:
    out = Path(sys.argv[1])
    home = Path.home() / ".ava"
    report = observe(home, home / "source")
    out.write_text(json.dumps(report, indent=2) + "\n")
    print(
        json.dumps(
            {
                "result": report["result"],
                "checks": {k: v["ok"] for k, v in report["checks"].items()},
            }
        )
    )
    return 0 if report["result"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
