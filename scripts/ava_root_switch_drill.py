#!/usr/bin/env python
"""ava-root switch drill: session <-> root flip on a keeper-wired dev cluster (S3 prerequisite).

Drives ONE worktree cluster through the production switch sequence the S3
rollout will run, with the full fingerprint set the runbook/S3 doc expects:

  provision   ensure `AVA_PERMISSIONS_HELPER_SPAWN=true` (sanctioned --machine
              write) and one stop/start cycle so the helper-spawn commitment is
              live; ends stopped
  baseline    bare `ava start` (session mode): service face up, keeper answers,
              no root socket, watchdog-probe jobs registered, one session's
              pid is a child of the keeper (helper spawn active)
  flip-on     `ava config set root_driver_enabled=true --machine M`; remote
              read-back + offline `.env` witness
  retire      `ava cluster watchdog-probe-unregister` for both roles (the S3
              sequence step; idempotent) -- jobs verified gone
  stop-old    `AVA_ROOT_DRIVER_ENABLED=0 ava stop` (pin = the mode the tree was
              started with: the stop-with-start-mode discipline)
  start-root  bare `ava start` (root mode): the keeper seeds ava-root; asserts
              keeper state/pid, root socket, tree == manifest, unit->root->
              keeper->1 parentage for EVERY unit, health rounds land,
              watchdog-probe jobs STILL absent (the converge gate retired
              them), no watchdog sessions survive
  flip-off    `ava config set root_driver_enabled=false --machine M` + witness
  stop-root   `AVA_ROOT_DRIVER_ENABLED=1 ava stop` (pin = root mode; stops
              through the keeper's root_stop)
  start-old   bare `ava start` (session mode): service face + watchdog
              sessions back, probe jobs re-registered (the gate's restore)
  cycle-old   one more session stop -> start under the helper state (the
              P1->P2 rollback premise: the old driver still stops/starts)
  [--teardown] `ava cluster destroy --path H --drop-db` + the dev keeper job
              (bootout + plist removal). The keeper is NOT covered by destroy
              today -- a known gap left for W1.4/S4 evaluation; this drill
              performs the cleanup explicitly and records it.

Preconditions: `scripts/install.sh --worktree` has birthed the home. Dev-only:
the cluster is a throwaway worktree home, jobs are dev-slug, nothing touches
production. Usage:

    .venv/bin/python scripts/ava_root_switch_drill.py \
        --workdir ~/.ava/workspaces/<id>/switch-drill-<ts> \
        --home ~/.ava-<worktree-dir> [--teardown]

Runs from a plain session env as well as `env -i`: `services.*` is only ever
imported in sanitized children (`_minimal_env`), never in this process, so a
launcher session's AVA_HOME cannot break the run. A `--workdir` under /tmp is
refused.
"""

from __future__ import annotations

import argparse
import json
import os
import plistlib
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import NoReturn, cast

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

_JOBS_SUFFIXES = ("watchdog-probe.gateway", "watchdog-probe.agent-runner")
_SOCKET_NAME = "ava-root.sock"
_START_TIMEOUT_S = 900.0
_STOP_TIMEOUT_S = 600.0
_HEALTH_WAIT_S = 150.0
_POLL_S = 2.0
_TRIMMED_SERVICES = ("milvus", "browser", "browser-mcp", "computer-mcp", "otel-collector")
_MIN_AVAILABLE_BYTES = 800 * 1024 * 1024
_MAX_LOAD_PER_CORE = 3.0


class _DrillError(RuntimeError):
    """A drill phase failed."""


def _fail(phase: str, detail: str) -> NoReturn:
    raise _DrillError(f"[{phase}] {detail}")


def _phase_pass(phase: str, detail: str) -> None:
    print(f"PHASE {phase}: PASS -- {detail}", flush=True)


def _save(evidence: Path, name: str, payload: object) -> None:
    (evidence / name).write_text(
        json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8"
    )


# ─── process/env plumbing ───────────────────────────────────────────────────


def _minimal_env(home: Path, *, root_pin: bool | None = None) -> dict[str, str]:
    """Allowlisted child env: no ambient AVA_* leaks into the dev cluster."""
    env = {
        "PATH": f"{REPO_ROOT / '.venv' / 'bin'}:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        "HOME": str(Path.home()),
        "LANG": "en_US.UTF-8",
        "TMPDIR": tempfile.gettempdir(),
        "AVA_HOME": str(home),
        # The job surface (dev keeper + probe jobs) is the object of this drill;
        # pin it on explicitly so a stray dev-.env `false` cannot silently skip it.
        "AVA_OS_JOBS_ENABLED": "true",
    }
    if root_pin is not None:
        env["AVA_ROOT_DRIVER_ENABLED"] = "1" if root_pin else "0"
    return env


def _run_ava(
    repo: Path,
    home: Path,
    args: list[str],
    *,
    root_pin: bool | None = None,
    timeout: float,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 -- this checkout's own console script
        [str(repo / ".venv" / "bin" / "ava"), *args],
        cwd=repo,
        env=_minimal_env(home, root_pin=root_pin),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _ava_must(
    repo: Path,
    home: Path,
    args: list[str],
    *,
    root_pin: bool | None = None,
    timeout: float,
    evidence: Path,
    tag: str,
) -> str:
    result = _run_ava(repo, home, args, root_pin=root_pin, timeout=timeout)
    blob = (
        f"$ ava {' '.join(args)}  [pin={root_pin}]\nrc={result.returncode}\n\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}\n"
    )
    (evidence / f"{tag}.out").write_text(blob, encoding="utf-8")
    if result.returncode != 0:
        raise _DrillError(f"`ava {' '.join(args)}` exited {result.returncode}; see {tag}.out")
    return result.stdout


# ─── cluster reads ──────────────────────────────────────────────────────────


def _machine_name(home: Path) -> str:
    """The cluster's machine name: the identity file once first-started, else
    the install-time `AVA_MACHINE_NAME` in `.env` (the file lands on first start)."""
    name_file = home / "machine_name"
    if name_file.exists():
        name = name_file.read_text(encoding="utf-8").strip()
        if name:
            return name
    from dotenv import dotenv_values

    raw = dotenv_values(home / ".env").get("AVA_MACHINE_NAME")
    name = str(raw).strip().strip("'") if raw is not None else ""
    if not name:
        raise _DrillError(f"no machine name in {name_file} or {home / '.env'}")
    return name


def _keeper_call(home: Path, expression: str) -> object:
    """Query the dev keeper over its own socket (subprocess with the dev home env)."""
    code = (
        "import json\n"
        "from services.permissions_helper import client\n"
        f"print(json.dumps({expression}))\n"
    )
    result = subprocess.run(  # noqa: S603 -- fixed argv
        [sys.executable, "-c", code],
        cwd=REPO_ROOT,
        env=_minimal_env(home),
        capture_output=True,
        text=True,
        timeout=30.0,
        check=False,
    )
    if result.returncode != 0:
        raise _DrillError(f"keeper call failed: {result.stderr.strip() or result.stdout.strip()}")
    parsed: object = json.loads(result.stdout)
    return parsed


def _keeper_status_or_none(home: Path) -> dict[str, object] | None:
    try:
        status = _keeper_call(home, "client.root_status()")
    except _DrillError:
        return None
    return cast("dict[str, object]", status)


def _root_status(home: Path) -> dict[str, object] | None:
    """Query the root daemon over its control socket (session-safe subprocess).

    Out-of-process like `_keeper_call`: children get `_minimal_env(home)` while
    this process's env is the launcher's, and an in-process `services.*` import
    would resolve that launcher home and raise against a conflicting session
    AVA_HOME (QA 3242 repro, 16:45).
    """
    socket_path = home / "run" / "ava-root" / _SOCKET_NAME
    code = (
        "import json\n"
        "from services.ava_root.client import RootClient, RootClientError\n"
        "try:\n"
        f"    response = RootClient({str(socket_path)!r}, timeout=5.0).status()\n"
        "except RootClientError:\n"
        "    response = None\n"
        "result = None if response is None else response.get('result')\n"
        "ok = bool(response) and bool(response.get('ok')) and isinstance(result, dict)\n"
        "print(json.dumps({'ok': ok, 'result': result if ok else None}))\n"
    )
    result = subprocess.run(  # noqa: S603 -- fixed argv
        [sys.executable, "-c", code],
        cwd=REPO_ROOT,
        env=_minimal_env(home),
        capture_output=True,
        text=True,
        timeout=30.0,
        check=False,
    )
    if result.returncode != 0:
        raise _DrillError(f"root status failed: {result.stderr.strip() or result.stdout.strip()}")
    parsed = cast("dict[str, object]", json.loads(result.stdout))
    return cast("dict[str, object]", parsed["result"]) if parsed.get("ok") else None


def _units(status: dict[str, object] | None) -> dict[str, dict[str, object]]:
    units: dict[str, dict[str, object]] = {}
    rows = status.get("units") if status else None
    for row in rows if isinstance(rows, list) else []:
        if isinstance(row, dict) and isinstance(row.get("id"), str):
            units[str(row["id"])] = row
    return units


def _root_pid(status: dict[str, object]) -> int | None:
    root = status.get("root")
    pid = cast("dict[str, object]", root).get("pid") if isinstance(root, dict) else None
    return pid if isinstance(pid, int) else None


_CONFIG_RETRY_S = 150.0
_CONFIG_RETRY_EVERY_S = 5.0


def _config_retry(
    repo: Path,
    home: Path,
    args: list[str],
    *,
    timeout: float,
    evidence: Path,
    tag: str,
) -> str:
    """A machine-addressed config call with bounded retries.

    The remote-op path can answer 503 for the first calls after a start (the
    ops side of the route is not warm yet); treat any failure as retryable up
    to the deadline and let the final attempt raise through `_ava_must`.
    """
    deadline = time.monotonic() + _CONFIG_RETRY_S
    attempt = 0
    while True:
        attempt += 1
        result = _run_ava(repo, home, args, timeout=timeout)
        blob = (
            f"$ ava {' '.join(args)}\nattempt={attempt} rc={result.returncode}\n\n"
            f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}\n"
        )
        (evidence / f"{tag}.out").write_text(blob, encoding="utf-8")
        if result.returncode == 0:
            return result.stdout
        if time.monotonic() >= deadline:
            raise _DrillError(
                f"`ava {' '.join(args)}` still failing after {attempt} attempt(s); see {tag}.out"
            )
        time.sleep(_CONFIG_RETRY_EVERY_S)


def _config_get(repo: Path, home: Path, key: str, machine: str, evidence: Path, tag: str) -> str:
    out = _config_retry(
        repo,
        home,
        ["config", "get", key, "--machine", machine],
        timeout=60.0,
        evidence=evidence,
        tag=tag,
    )
    first = out.splitlines()[0] if out.splitlines() else ""
    _, _, raw = first.partition(" = ")
    return raw.strip()


def _config_set(
    repo: Path, home: Path, key: str, value: str, machine: str, evidence: Path, tag: str
) -> None:
    _config_retry(
        repo,
        home,
        ["config", "set", f"{key}={value}", "--machine", machine],
        timeout=120.0,
        evidence=evidence,
        tag=tag,
    )


def _env_witness(home: Path, key: str) -> str | None:
    """Offline witness: the value as written in this home's `.env` file."""
    from dotenv import dotenv_values

    env_path = home / ".env"
    if not env_path.exists():
        return None
    return dotenv_values(env_path).get(key)


def _truthy(raw: str | None) -> bool:
    return str(raw).strip().strip("'").lower() in {"true", "1", "yes"}


# ─── jobs / sessions ────────────────────────────────────────────────────────


def _job_labels(home: Path) -> list[str]:
    """This cluster's watchdog-probe labels: plists whose AVA_HOME is this home."""
    labels: list[str] = []
    for suffix in _JOBS_SUFFIXES:
        for plist in sorted(
            (Path.home() / "Library" / "LaunchAgents").glob(f"com.ava.*{suffix}.plist")
        ):
            try:
                data = plistlib.loads(plist.read_bytes())
            except (OSError, plistlib.InvalidFileException):
                continue
            env = data.get("EnvironmentVariables") or {}
            if str(env.get("AVA_HOME", "")) == str(home):
                labels.append(str(data.get("Label", plist.stem)))
    return labels


def _launchctl_has(label: str) -> bool:
    result = subprocess.run(["launchctl", "list"], capture_output=True, text=True, check=False)
    return any(line.split()[-1] == label for line in result.stdout.splitlines() if line.split())


def _session_records(home: Path) -> list[str]:
    return sorted(record.stem for record in (home / "run" / "sessions").glob("*.json"))


def _helper_sessions(home: Path) -> list[dict[str, object]]:
    try:
        rows = _keeper_call(home, "client.session_list()")
    except _DrillError:
        return []
    return [cast("dict[str, object]", row) for row in cast("list[object]", rows)]


def _watchdog_sessions(home: Path) -> list[str]:
    records = [name for name in _session_records(home) if "watchdog" in name]
    helper = [
        str(row.get("name", ""))
        for row in _helper_sessions(home)
        if "watchdog" in str(row.get("name", ""))
    ]
    return sorted(set(records) | set(helper))


def _status_rows(output: str) -> dict[str, str]:
    """The service face as `{ava-<service>: "<session-mark><probe-mark>"}`."""
    import re

    rows: dict[str, str] = {}
    pattern = re.compile(r"^(ava-[a-z0-9-]+)\s+(\S)\s+(\S) \(")
    for line in output.splitlines():
        found = pattern.match(line)
        if found:
            rows[found.group(1)] = found.group(2) + found.group(3)
    return rows


def _wait_health_rounds(home: Path) -> dict[str, object]:
    deadline = time.monotonic() + _HEALTH_WAIT_S
    while time.monotonic() < deadline:
        status = _root_status(home)
        health = (status or {}).get("health")
        if (
            isinstance(health, dict)
            and health
            and all(isinstance(row, dict) and row.get("last_verdict") for row in health.values())
        ):
            return cast("dict[str, object]", status)
        time.sleep(_POLL_S)
    _fail("start-root", "health verdicts did not land within the window")


def _parent_chain(pid: int) -> list[tuple[int, str]]:
    import psutil

    chain: list[tuple[int, str]] = []
    process = psutil.Process(pid)
    while process is not None and process.pid > 1:
        chain.append((process.pid, process.name()))
        parent = process.parent()
        process = parent
    return chain


def _resource_guard(evidence: Path, tag: str) -> None:
    import psutil

    line = (
        f"loadavg={os.getloadavg()} available={psutil.virtual_memory().available / 1024**3:.2f}GiB"
    )
    with (evidence / "resources.log").open("a", encoding="utf-8") as handle:
        handle.write(f"[{tag}] {line}\n")
    print(f"  resources[{tag}]: {line}")
    if psutil.virtual_memory().available < _MIN_AVAILABLE_BYTES:
        _fail(tag, "resource circuit breaker: available memory low")
    if os.getloadavg()[0] > _MAX_LOAD_PER_CORE * (os.cpu_count() or 1):
        _fail(tag, "resource circuit breaker: load high")


def _disable_args() -> list[str]:
    args: list[str] = []
    for service in _TRIMMED_SERVICES:
        args += ["--disable-service", service]
    return args


# ─── phases ─────────────────────────────────────────────────────────────────


def _phase_normalize(repo: Path, home: Path, evidence: Path, machine: str) -> None:
    """Park a rerun's leftover root mode and end stopped with the switch off.

    A rerun after a mid-drill failure can begin in root mode (the switch was
    already written); the drill's entry state is session mode, stopped. A no-op
    on a clean start.
    """
    witness = _env_witness(home, "AVA_ROOT_DRIVER_ENABLED")
    if not _truthy(witness):
        return
    print("  normalize: root switch is on from a previous run; reverting")
    _ava_must(
        repo,
        home,
        ["start", *_disable_args()],
        timeout=_START_TIMEOUT_S,
        evidence=evidence,
        tag="normalize-start",
    )
    _config_set(repo, home, "root_driver_enabled", "false", machine, evidence, "normalize-set")
    _ava_must(
        repo,
        home,
        ["stop", "--yes"],
        root_pin=True,
        timeout=_STOP_TIMEOUT_S,
        evidence=evidence,
        tag="normalize-stop",
    )
    _phase_pass("normalize", "root switch reverted; stopped")


def _phase_preflight(home: Path, evidence: Path) -> str:
    if not home.is_dir() or not (home / ".env").is_file():
        _fail(
            "preflight",
            f"{home} is not an installed cluster home -- run `scripts/install.sh --worktree` first",
        )
    if "prod" in home.name or home == Path.home() / ".ava":
        _fail("preflight", f"{home} looks like a production home; this drill is dev-only")
    _resource_guard(evidence, "preflight")
    machine = _machine_name(home)
    print(f"  home={home} machine={machine}")
    return machine


def _phase_provision(repo: Path, home: Path, evidence: Path, machine: str) -> None:
    """SPAWN=true + one restart so the helper-spawn commitment is live; end stopped."""
    keeper = _keeper_status_or_none(home)
    if keeper is None:
        print("  keeper not answering yet -- first session start installs it")
        _ava_must(
            repo,
            home,
            ["start", *_disable_args()],
            timeout=_START_TIMEOUT_S,
            evidence=evidence,
            tag="provision-first-start",
        )
        _ava_must(
            repo,
            home,
            ["stop", "--yes"],
            timeout=_STOP_TIMEOUT_S,
            evidence=evidence,
            tag="provision-first-stop",
        )
    # The spawn flag is a --machine write and the write path needs the cluster
    # up (gateway + ops) -- get it while the start below holds the cluster up.
    _ava_must(
        repo,
        home,
        ["start", *_disable_args()],
        timeout=_START_TIMEOUT_S,
        evidence=evidence,
        tag="provision-probe-start",
    )
    spawn_raw = _config_get(
        repo, home, "permissions_helper_spawn", machine, evidence, "provision-spawn-get"
    )
    changed = False
    if not _truthy(spawn_raw):
        print(f"  permissions_helper_spawn={spawn_raw!r}; setting true via --machine")
        _config_set(
            repo, home, "permissions_helper_spawn", "true", machine, evidence, "provision-spawn-set"
        )
        changed = True
    _ava_must(
        repo,
        home,
        ["stop", "--yes"],
        timeout=_STOP_TIMEOUT_S,
        evidence=evidence,
        tag="provision-spawn-stop",
    )
    _ava_must(
        repo,
        home,
        ["start", *_disable_args()],
        timeout=_START_TIMEOUT_S,
        evidence=evidence,
        tag="provision-spawn-start",
    )
    session_procs = _helper_sessions(home)
    if not session_procs:
        _fail(
            "provision",
            "helper session_list is empty after a helper-spawn start (commitment not live)",
        )
    keeper = _keeper_status_or_none(home)
    if keeper is None:
        _fail("provision", "keeper stopped answering after the start")
    keeper_pid = None
    for row in session_procs:
        pid = row.get("pid")
        if isinstance(pid, int):
            chain = _parent_chain(pid)
            if len(chain) >= 2 and chain[1][1].startswith("AvaPermissionsHelper"):
                keeper_pid = chain[1][0]
                break
    if keeper_pid is None:
        _fail("provision", f"no helper-owned session found (procs={session_procs})")
    print(f"  helper-spawn live: session proc under keeper pid {keeper_pid} (changed={changed})")
    _ava_must(
        repo,
        home,
        ["stop", "--yes"],
        timeout=_STOP_TIMEOUT_S,
        evidence=evidence,
        tag="provision-final-stop",
    )


def _phase_baseline(repo: Path, home: Path, evidence: Path) -> dict[str, str]:
    _resource_guard(evidence, "baseline")
    _ava_must(
        repo,
        home,
        ["start", *_disable_args()],
        timeout=_START_TIMEOUT_S,
        evidence=evidence,
        tag="baseline-start",
    )
    status_out = _ava_must(
        repo, home, ["status"], timeout=_STOP_TIMEOUT_S, evidence=evidence, tag="baseline-status"
    )
    face = _status_rows(status_out)
    if len(face) < 3:
        _fail("baseline", f"service face too small: {sorted(face)}")
    if _root_status(home) is not None:
        _fail("baseline", "a root answers while the switch is off")
    labels = _job_labels(home)
    if len(labels) < 2:
        _fail("baseline", f"watchdog-probe jobs not registered ({labels})")
    watchdogs = _watchdog_sessions(home)
    if not watchdogs:
        _fail("baseline", "no watchdog sessions recorded in session mode")
    _save(
        evidence, "baseline.face.json", {"face": face, "job_labels": labels, "watchdogs": watchdogs}
    )
    _phase_pass("baseline", f"face={len(face)} jobs={len(labels)} watchdogs={watchdogs}")
    return face


def _phase_flip_on(repo: Path, home: Path, evidence: Path, machine: str) -> None:
    _config_set(repo, home, "root_driver_enabled", "true", machine, evidence, "flip-on-set")
    back = _config_get(repo, home, "root_driver_enabled", machine, evidence, "flip-on-get")
    witness = _env_witness(home, "AVA_ROOT_DRIVER_ENABLED")
    if not _truthy(back):
        _fail("flip-on", f"read-back is {back!r}")
    if not _truthy(witness):
        _fail("flip-on", f"offline witness is {witness!r}")
    _save(evidence, "flip-on.json", {"read_back": back, "witness": witness})
    _phase_pass("flip-on", f"read_back={back!r} witness={witness!r}")


def _phase_retire(repo: Path, home: Path, evidence: Path) -> None:
    before = _job_labels(home)
    for role in ("gateway", "agent-runner"):
        _ava_must(
            repo,
            home,
            ["cluster", "watchdog-probe-unregister", "--role", role],
            timeout=60.0,
            evidence=evidence,
            tag=f"retire-{role}",
        )
    after = _job_labels(home)
    if after:
        _fail("retire", f"probe jobs still present: {after}")
    stragglers = [label for label in before if _launchctl_has(label)]
    if stragglers:
        _fail("retire", f"labels still in launchctl: {stragglers}")
    _save(evidence, "retire.json", {"before": before, "after": after})
    _phase_pass("retire", f"retired {before}")


def _phase_stop_old(repo: Path, home: Path, evidence: Path) -> None:
    _ava_must(
        repo,
        home,
        ["stop", "--yes"],
        root_pin=False,
        timeout=_STOP_TIMEOUT_S,
        evidence=evidence,
        tag="stop-old",
    )
    if _root_status(home) is not None:
        _fail("stop-old", "root still answers after stop")
    sessions = [name for name in _session_records(home) if "watchdog" not in name]
    if sessions:
        _fail("stop-old", f"service sessions still recorded: {sessions}")
    _phase_pass("stop-old", "session-mode stop clean")


def _assert_agent_host_stable(home: Path, evidence: Path, *, window_s: float = 45.0) -> None:
    """root + helper-spawn must not churn agent-host (task #3402 regression watch).

    The direct-parent guard bug looped agent-host every ~30s ("permissions
    helper parent chain broken" -> os._exit(70) -> root restart). Watch the pid
    and the log marker across one window; either moving is a failure.
    """
    log = home / "run" / "ava-root" / "logs" / "agent-host" / "output.log"
    marker = "self-terminating for helper respawn"

    def sample() -> tuple[int | None, int]:
        status = _root_status(home) or {}
        row = _units(status).get("agent-host") or {}
        pid = row.get("pid")
        count = 0
        if log.exists():
            count = log.read_text(errors="replace").count(marker)
        return (pid if isinstance(pid, int) else None), count

    pid0, count0 = sample()
    if pid0 is None:
        _fail("start-root", "agent-host has no pid to watch for stability")
    time.sleep(window_s)
    pid1, count1 = sample()
    if pid1 != pid0 or count1 > count0:
        tail = ""
        if log.exists():
            tail = " | ".join(log.read_text(errors="replace").splitlines()[-4:])
        _fail(
            "start-root",
            "agent-host churned under root+helper-spawn (task #3402 regression): "
            f"pid {pid0} -> {pid1}, self-terminations {count0} -> {count1}; tail: {tail}",
        )
    _save(
        evidence,
        "start-root.agent-host-stability.json",
        {"pid": pid1, "window_s": window_s, "self_terminations": count1},
    )


def _phase_start_root(repo: Path, home: Path, evidence: Path) -> dict[str, object]:
    _resource_guard(evidence, "start-root")
    _ava_must(
        repo,
        home,
        ["start", *_disable_args()],
        timeout=_START_TIMEOUT_S,
        evidence=evidence,
        tag="start-root",
    )
    keeper = _keeper_status_or_none(home)
    if keeper is None or keeper.get("state") != "running":
        _fail("start-root", f"keeper state {keeper!r} != running")
    keeper_pid = keeper.get("pid")
    if not isinstance(keeper_pid, int):
        _fail("start-root", f"keeper has no root pid: {keeper!r}")
    status = _root_status(home)
    if status is None:
        _fail("start-root", "no root answers on the control socket")
    root_pid = _root_pid(status)
    if not isinstance(root_pid, int):
        _fail("start-root", "no root pid in the root's status")
    if root_pid != keeper_pid:
        _fail("start-root", f"root pid {root_pid} != keeper's carried pid {keeper_pid}")
    units = _units(status)
    manifest = json.loads(
        (home / "run" / "ava-root" / "manifests.json").read_text(encoding="utf-8")
    )
    manifest_ids = {str(unit["id"]) for unit in cast("list[dict[str, object]]", manifest["units"])}
    if set(units) != manifest_ids:
        _fail("start-root", f"tree != manifest: missing {sorted(manifest_ids - set(units))}")
    if manifest_ids & {"gateway-watchdog", "agent-runner-watchdog"}:
        _fail("start-root", "watchdogs must not be tree units")
    parentage: dict[str, list[tuple[int, str]]] = {}
    for unit_id, row in sorted(units.items()):
        pid = row.get("pid")
        if row.get("state") != "running":
            _fail("start-root", f"unit {unit_id} not running: {row}")
        if not isinstance(pid, int):
            _fail("start-root", f"unit {unit_id} has no pid: {row}")
        chain = _parent_chain(pid)
        parentage[unit_id] = chain
        if len(chain) < 2 or chain[1][0] != root_pid:
            _fail("start-root", f"unit {unit_id} ppid chain is not root-parented: {chain}")
    root_chain = _parent_chain(root_pid)
    if len(root_chain) < 2 or not root_chain[1][1].startswith("AvaPermissionsHelper"):
        _fail("start-root", f"root's own parent is not the keeper: {root_chain}")
    labels = _job_labels(home)
    if labels:
        _fail("start-root", f"probe jobs present after the root-mode start: {labels}")
    watchdogs = _watchdog_sessions(home)
    if watchdogs:
        _fail("start-root", f"watchdog sessions present under the root tree: {watchdogs}")
    _wait_health_rounds(home)
    _assert_agent_host_stable(home, evidence)
    _save(evidence, "start-root.status.json", status)
    _save(evidence, "start-root.parentage.json", parentage)
    _phase_pass(
        "start-root",
        f"root={root_pid} units={len(units)} parentage ok; probes still retired; no watchdog sessions",
    )
    return status


def _phase_flip_off(repo: Path, home: Path, evidence: Path, machine: str) -> None:
    _config_set(repo, home, "root_driver_enabled", "false", machine, evidence, "flip-off-set")
    back = _config_get(repo, home, "root_driver_enabled", machine, evidence, "flip-off-get")
    witness = _env_witness(home, "AVA_ROOT_DRIVER_ENABLED")
    if _truthy(back) or _truthy(witness):
        _fail("flip-off", f"still on: read_back={back!r} witness={witness!r}")
    _phase_pass("flip-off", f"read_back={back!r} witness={witness!r}")


def _phase_stop_root(repo: Path, home: Path, evidence: Path) -> None:
    _ava_must(
        repo,
        home,
        ["stop", "--yes"],
        root_pin=True,
        timeout=_STOP_TIMEOUT_S,
        evidence=evidence,
        tag="stop-root",
    )
    if _root_status(home) is not None:
        _fail("stop-root", "root still answers after the root-mode stop")
    keeper = _keeper_status_or_none(home)
    if keeper is not None and keeper.get("state") == "running":
        _fail("stop-root", f"keeper still running: {keeper!r}")
    _phase_pass("stop-root", f"keeper state after stop: {(keeper or {}).get('state')!r}")


def _phase_start_old(repo: Path, home: Path, evidence: Path, tag: str) -> dict[str, str]:
    _ava_must(
        repo,
        home,
        ["start", *_disable_args()],
        timeout=_START_TIMEOUT_S,
        evidence=evidence,
        tag=f"{tag}-start",
    )
    status_out = _ava_must(
        repo, home, ["status"], timeout=_STOP_TIMEOUT_S, evidence=evidence, tag=f"{tag}-status"
    )
    face = _status_rows(status_out)
    if len(face) < 3:
        _fail(tag, f"service face too small: {sorted(face)}")
    labels = _job_labels(home)
    if len(labels) < 2:
        _fail(tag, f"converge did not restore probe jobs (labels={labels})")
    watchdogs = _watchdog_sessions(home)
    if not watchdogs:
        _fail(tag, "watchdog sessions did not come back")
    _phase_pass(tag, f"face={len(face)} jobs={len(labels)} watchdogs={watchdogs}")
    return face


def _phase_cycle_old(repo: Path, home: Path, evidence: Path) -> None:
    _ava_must(
        repo,
        home,
        ["stop", "--yes"],
        timeout=_STOP_TIMEOUT_S,
        evidence=evidence,
        tag="cycle-old-stop",
    )
    _ava_must(
        repo,
        home,
        ["start", *_disable_args()],
        timeout=_START_TIMEOUT_S,
        evidence=evidence,
        tag="cycle-old-start",
    )
    status_out = _ava_must(
        repo, home, ["status"], timeout=_STOP_TIMEOUT_S, evidence=evidence, tag="cycle-old-status"
    )
    if len(_status_rows(status_out)) < 3:
        _fail("cycle-old", "face missing after the old-mode cycle")
    _phase_pass("cycle-old", "session stop->start clean under the helper state")


def _keeper_plists(home: Path) -> list[tuple[str, Path]]:
    """(label, plist) of keeper jobs pointing at this home — socket-anchored.

    The keeper plist carries no AVA_HOME; its identity is the
    AVA_PERMISSIONS_HELPER_SOCKET path under this home (the lifecycle writer's
    convention). The prod cluster's plist never matches a dev home.
    """
    prefix = str(home / "run" / "permissions-helper.")
    found: list[tuple[str, Path]] = []
    for plist in sorted(
        (Path.home() / "Library" / "LaunchAgents").glob("com.ava.*permissions-helper*.plist")
    ):
        try:
            data = plistlib.loads(plist.read_bytes())
        except (OSError, plistlib.InvalidFileException):
            continue
        env = data.get("EnvironmentVariables") or {}
        if str(env.get("AVA_PERMISSIONS_HELPER_SOCKET", "")).startswith(prefix):
            found.append((str(data.get("Label", plist.stem)), plist))
    return found


def _phase_teardown(repo: Path, home: Path, evidence: Path) -> None:
    """destroy + the keeper cleanup destroy does not do (known gap, W1.4/S4)."""
    import psutil

    keepers = _keeper_plists(home)
    keeper_labels = [label for label, _ in keepers]
    keeper_pid = None
    if keeper_labels:
        for line in subprocess.run(
            ["launchctl", "list"], capture_output=True, text=True, check=False
        ).stdout.splitlines():
            parts = line.split()
            if len(parts) >= 3 and parts[2] in keeper_labels:
                keeper_pid = int(parts[0]) if parts[0].isdigit() else None
    _ava_must(
        repo,
        home,
        ["cluster", "destroy", "--path", str(home), "--drop-db"],
        timeout=_STOP_TIMEOUT_S,
        evidence=evidence,
        tag="teardown-destroy",
    )
    cleanup: dict[str, object] = {"keeper_labels": keeper_labels, "keeper_pid": keeper_pid}
    uid = os.getuid()
    for label, plist in keepers:
        subprocess.run(  # noqa: S603 -- fixed argv
            ["launchctl", "bootout", f"gui/{uid}/{label}"],
            capture_output=True,
            text=True,
            check=False,
        )
        plist.unlink(missing_ok=True)
    if keeper_pid is not None:
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline and psutil.pid_exists(keeper_pid):
            time.sleep(0.25)
        cleanup["keeper_process_gone"] = not psutil.pid_exists(keeper_pid)
    problems: list[str] = []
    if _job_labels(home):
        problems.append(f"probe plists left: {_job_labels(home)}")
    leftover = _keeper_plists(home)
    if leftover:
        problems.append(f"keeper plists left: {[str(path) for _, path in leftover]}")
    registered = [label for label in keeper_labels if _launchctl_has(label)]
    if registered:
        problems.append(f"keeper labels still registered: {registered}")
    cleanup["problems"] = problems
    _save(evidence, "teardown.json", cleanup)
    if problems:
        _fail("teardown", "; ".join(problems))
    _phase_pass("teardown", f"destroyed; keeper cleanup {cleanup}")


def _workdir_rejection(workdir: Path) -> str | None:
    """Validation error for the evidence workdir, or None when usable.

    Evidence must persist across review; `/tmp` is swept, so the help's
    "not /tmp" is enforced here rather than merely promised.
    """
    if not workdir.is_absolute():
        return "workdir must be absolute"
    # Refusing /tmp is this check's point; S108 concerns creating temp files.
    tmp_root = Path("/tmp").resolve()  # noqa: S108
    resolved = workdir.resolve()
    if resolved == tmp_root or tmp_root in resolved.parents:
        return "workdir must not be under /tmp"
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ava-root-switch-drill", description=(__doc__ or "").splitlines()[0]
    )
    parser.add_argument(
        "--workdir", required=True, type=Path, help="evidence root (absolute, not /tmp)"
    )
    parser.add_argument("--home", required=True, type=Path, help="the worktree cluster home")
    parser.add_argument("--repo", type=Path, default=REPO_ROOT)
    parser.add_argument(
        "--skip-provision", action="store_true", help="assume SPAWN=true is already live"
    )
    parser.add_argument(
        "--teardown", action="store_true", help="destroy + keeper cleanup at the end"
    )
    args = parser.parse_args(argv)

    workdir_error = _workdir_rejection(args.workdir)
    if workdir_error:
        print(f"FAIL(args): {workdir_error}", file=sys.stderr)
        return 1
    evidence = args.workdir / "evidence"
    evidence.mkdir(parents=True, exist_ok=True)
    home = args.home.expanduser()

    phases: list[tuple[str, str]] = []
    try:
        machine = _phase_preflight(home, evidence)
        phases.append(("preflight", "PASS"))
        _phase_normalize(args.repo, home, evidence, machine)
        if not args.skip_provision:
            _phase_provision(args.repo, home, evidence, machine)
            phases.append(("provision", "PASS"))
        _phase_baseline(args.repo, home, evidence)
        phases.append(("baseline", "PASS"))
        _phase_flip_on(args.repo, home, evidence, machine)
        phases.append(("flip-on", "PASS"))
        _phase_retire(args.repo, home, evidence)
        phases.append(("retire", "PASS"))
        _phase_stop_old(args.repo, home, evidence)
        phases.append(("stop-old", "PASS"))
        _phase_start_root(args.repo, home, evidence)
        phases.append(("start-root", "PASS"))
        _phase_flip_off(args.repo, home, evidence, machine)
        phases.append(("flip-off", "PASS"))
        _phase_stop_root(args.repo, home, evidence)
        phases.append(("stop-root", "PASS"))
        _phase_start_old(args.repo, home, evidence, "start-old")
        phases.append(("start-old", "PASS"))
        _phase_cycle_old(args.repo, home, evidence)
        phases.append(("cycle-old", "PASS"))
        if args.teardown:
            _phase_teardown(args.repo, home, evidence)
            phases.append(("teardown", "PASS"))
    except _DrillError as exc:
        print(f"DRILL FAILED: {exc}", file=sys.stderr)
        _save(evidence, "phases.json", [*phases, ("failed", str(exc))])
        return 1
    _save(evidence, "phases.json", phases)
    print(f"SWITCH DRILL: {len(phases)}/{len(phases)} phases PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
