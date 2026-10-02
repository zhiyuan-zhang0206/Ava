"""In-boundary observer for the verification recipes (Linux container, Tart macOS guest).

Runs inside the boundary after `ava start`, from the checkout's own interpreter:

    cd ~/.ava/source && PYTHONPATH=. .venv/bin/python observe.py OUT.json

It checks the started cluster from the outside, the way an operator would, and
writes one JSON document with every check's outcome. A failed check is recorded and
the remaining checks still run, so a partial result keeps its evidence; the exit
status is nonzero when any check failed. A check that cannot apply on this platform
is listed under `not_applicable` with its reason: it neither passes nor fails.

- toolchain: the installed data-plane and runtime versions, Redis on the approved
  8.2 series, pgvector present (apt paths on Linux, the Homebrew kegs on macOS);
- source: the checkout is at the commit the run named and has no changed file;
- services: every service of the verification profile answers its identity probe;
- frontend: the app port answers over HTTP;
- cors: the gateway answers the frontend's exact browser origin, credentialed;
- scripted_agent: an agent driven by the scripted model has executed
  `print(1 + 2)` through the real gateway and agent host, and the recorded output
  body is `3` (not a model claim, not a digit in a timestamp);
- helper_chain (macOS only): the signed permissions helper answers with both desktop
  grants held, and the process tree is launchd -> helper -> ava-root -> every unit.
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

IS_MACOS = sys.platform == "darwin"
SERVICES = frozenset({"gateway", "frontend", "ops", "agent-host"})
PG_BIN = Path("/usr/lib/postgresql/17/bin")
TCC_DB = "/Library/Application Support/com.apple.TCC/TCC.db"
HELPER_TCC_QUERY = (
    "select service, auth_value from access where client = 'com.ava.permissions-helper'"
)
# The helper's ping, the two desktop grants it must hold, and the lifecycle protocols
# the start admits it by (cli/commands/lifecycle/root_driver.py).
HELPER_PING = {
    "pong": True,
    "preflight_screen": True,
    "ax_trusted": True,
    "root_stop_intent_v1": True,
    "helper_shutdown_v1": True,
}
# System TCC rows for the helper: allowed (auth_value 2) for both desktop services.
HELPER_TCC_ROWS = {"kTCCServiceScreenCapture": 2, "kTCCServiceAccessibility": 2}
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


def _toolchain_linux() -> Detail:
    versions = {
        "arch": _out(["uname", "-m"]),
        "python": _out([sys.executable, "--version"]),
        "uv": _out(["uv", "--version"]),
        "node": _out(["node", "--version"]),
        "postgres": _out([str(PG_BIN / "postgres"), "--version"]),
        "redis": _out(["redis-server", "--version"]).split(" sha=")[0],
        "pgbouncer": _out(["pgbouncer", "--version"]).splitlines()[0],
    }
    if not Path("/usr/share/postgresql/17/extension/vector.control").is_file():
        raise RuntimeError("pgvector is not installed for PostgreSQL 17")
    return {"versions": versions, "pgvector": "installed"}


def _toolchain_macos() -> Detail:
    """The Homebrew kegs, resolved the way the start resolves them."""
    from base.cluster.dataplane.pg_tools import brew_prefix, pg_tool
    from cli.commands.data_plane.pgbouncer import pgbouncer_bin

    postgres = pg_tool("postgres")
    versions = {
        "arch": _out(["uname", "-m"]),
        "macos": _out(["sw_vers", "-productVersion"]),
        "python": _out([sys.executable, "--version"]),
        "uv": _out(["uv", "--version"]),
        "node": _out(["node", "--version"]),
        "postgres": _out([str(postgres), "--version"]),
        "redis": _out([str(brew_prefix("redis@8.2") / "bin" / "redis-server"), "--version"]).split(
            " sha="
        )[0],
        "pgbouncer": _out([pgbouncer_bin(), "--version"]).splitlines()[0],
    }
    control = Path(_out([str(pg_tool("pg_config")), "--sharedir"])) / "extension" / "vector.control"
    if not control.is_file():
        raise RuntimeError(f"pgvector is not installed for PostgreSQL 17: no {control}")
    return {"versions": versions, "pgvector": str(control)}


def check_toolchain(_home: Path, _source: Path) -> Detail:
    detail = _toolchain_macos() if IS_MACOS else _toolchain_linux()
    versions = detail["versions"]
    if not re.search(r"\bv=8\.2\.\d+$", versions["redis"]):
        raise RuntimeError(f"Redis is not on the approved 8.2 series: {versions['redis']}")
    if "(PostgreSQL) 17." not in versions["postgres"]:
        raise RuntimeError(f"PostgreSQL is not 17: {versions['postgres']}")
    return detail


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


def ancestry_problem(chain: list[Detail], via: list[int]) -> str | None:
    """Why a process chain does not end `via[0] -> via[1] -> ... -> launchd`, or None.

    `chain` is the process then its parents, nearest first, ending at pid 1; `via` are
    the pids that must be the last links before launchd, each the parent of the one
    before it (anything may sit between the process itself and `via[0]`).
    """
    pids = [row["pid"] for row in chain]
    if pids[-1] != 1 or chain[-1]["name"] != "launchd":
        return f"the chain does not end at launchd: {chain}"
    if pids[len(pids) - 1 - len(via) : -1] != via:
        return f"the chain does not run through {via} just under launchd: {chain}"
    return None


def process_chain(pid: int) -> list[Detail]:
    """The process and its parents up to and including launchd, nearest first."""
    import psutil

    process = psutil.Process(pid)
    chain = [{"pid": p.pid, "name": p.name()} for p in (process, *process.parents())]
    return chain[: [row["pid"] for row in chain].index(1) + 1]


def _tree_problems(
    status: Detail, root_pid: int, helper_pid: int, chains: dict[str, Any]
) -> list[str]:
    """What is wrong with the root's units: each must run, sit under root and helper, and
    together they must be exactly the verification profile's services. Fills `chains`."""
    problems: list[str] = []
    for unit in status["units"]:
        if unit["state"] != "running":
            problems.append(f"unit {unit['id']} is {unit['state']}")
            continue
        chains[unit["id"]] = process_chain(unit["pid"])
        problems.append(ancestry_problem(chains[unit["id"]], [root_pid, helper_pid]) or "")
    units = frozenset(unit["id"] for unit in status["units"])
    if units != SERVICES:
        problems.append(f"the root runs units {sorted(units)}, expected {sorted(SERVICES)}")
    return [problem for problem in problems if problem]


def _helper_signature() -> list[str]:
    """The installed helper's designated requirement and CDHash (`codesign -d` prints the
    requirement on stdout and the signature details on stderr)."""
    from base.paths import permissions_helper_app_dir

    app = permissions_helper_app_dir() / "AvaPermissionsHelper.app"
    signing = subprocess.run(  # noqa: S603 — fixed read-only command, no shell
        ["codesign", "-dv", "-r-", "--verbose=4", str(app)],
        capture_output=True,
        text=True,
        timeout=20,
        check=True,
    )
    wanted = ("designated =>", "CDHash=")
    return [
        line for line in (signing.stdout + signing.stderr).splitlines() if line.startswith(wanted)
    ]


def check_helper_chain(_home: Path, _source: Path) -> Detail:
    """The signed helper holds both desktop grants and owns the tree: launchd -> helper
    -> ava-root -> every unit. Reads the helper, the root and the system TCC database;
    changes nothing."""
    from base.native_process.root_control.client import RootClient
    from base.paths import root_run_dir
    from services.permissions_helper import client as helper

    ping: Detail = dict(helper.ping())
    seen = {key: ping[key] for key in HELPER_PING}
    if seen != HELPER_PING:
        raise RuntimeError(f"the helper's ping is {seen}, expected {HELPER_PING}")
    helper_pid = ping["pid"]
    keeper: Detail = dict(helper.root_status())
    root_pid = keeper["pid"]
    if keeper["state"] != "running" or keeper["run_dir"] != str(root_run_dir()):
        raise RuntimeError(f"the helper does not keep this home's running root: {keeper}")
    reply: Detail = dict(RootClient(root_run_dir() / "ava-root.sock", timeout=10).status())
    status: Detail = reply["result"]
    if status["root"]["pid"] != root_pid:
        raise RuntimeError(f"the helper keeps root {root_pid}, the root reports {status['root']}")
    chains = {"helper": process_chain(helper_pid), "root": process_chain(root_pid)}
    problems = [
        ancestry_problem(chains["helper"], [helper_pid]),
        ancestry_problem(chains["root"], [root_pid, helper_pid]),
        *_tree_problems(status, root_pid, helper_pid, chains),
    ]
    if any(problems):
        raise RuntimeError("; ".join(problem for problem in problems if problem))
    rows = _out(["sqlite3", "-readonly", TCC_DB, HELPER_TCC_QUERY])
    tcc = {line.split("|")[0]: int(line.split("|")[1]) for line in rows.splitlines()}
    if tcc != HELPER_TCC_ROWS:
        raise RuntimeError(
            f"the system TCC rows for the helper are {tcc}, expected {HELPER_TCC_ROWS}"
        )
    return {"ping": ping, "chains": chains, "tcc": tcc, "signature": _helper_signature()}


CHECKS: dict[str, Callable[[Path, Path], Detail]] = {
    "toolchain": check_toolchain,
    "source": check_source,
    "services": check_services,
    "frontend": check_frontend,
    "cors": check_cors,
    "scripted_agent": check_scripted_agent,
    "helper_chain": check_helper_chain,
}
# Checks that exist on one platform only; any other check runs everywhere.
ONLY_ON_MACOS = {"helper_chain": "the permissions helper and launchd exist on macOS only"}


def applicable_checks(
    *,
    macos: bool,
) -> tuple[dict[str, Callable[[Path, Path], Detail]], dict[str, str]]:
    """The checks that run on this platform, and why each remaining one does not."""
    if macos:
        return dict(CHECKS), {}
    return (
        {name: check for name, check in CHECKS.items() if name not in ONLY_ON_MACOS},
        dict(ONLY_ON_MACOS),
    )


def _cgroup(name: str) -> str | None:
    """A cgroup v2 value of this container (memory.peak, memory.max), None when absent."""
    path = Path("/sys/fs/cgroup") / name
    return path.read_text().strip() if path.is_file() else None


def memory_report() -> Detail:
    """What the whole run needed: the container's cgroup on Linux, the guest's size and
    swap on macOS (a guest has no cgroup, so no peak is recorded there)."""
    if not IS_MACOS:
        return {"peak_bytes": _cgroup("memory.peak"), "limit_bytes": _cgroup("memory.max")}
    return {
        "peak_bytes": None,
        "not_applicable": "memory.peak is a Linux cgroup value",
        "hw_memsize_bytes": int(_out(["sysctl", "-n", "hw.memsize"])),
        "swap": _out(["sysctl", "-n", "vm.swapusage"]),
    }


def observe(home: Path, source: Path) -> Detail:
    """Run every applicable check, recording each outcome; never stop at the first failure."""
    runnable, not_applicable = applicable_checks(macos=IS_MACOS)
    checks: dict[str, Detail] = {}
    for name, check in runnable.items():
        started = time.monotonic()
        try:
            checks[name] = {"ok": True, "detail": check(home, source)}
        except Exception as error:
            checks[name] = {"ok": False, "error": f"{type(error).__name__}: {error}"}
        checks[name]["seconds"] = round(time.monotonic() - started, 3)
    return {
        "at": time.time(),
        "platform": sys.platform,
        "result": "passed" if all(row["ok"] for row in checks.values()) else "failed",
        "memory": memory_report(),
        "checks": checks,
        "not_applicable": not_applicable,
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
