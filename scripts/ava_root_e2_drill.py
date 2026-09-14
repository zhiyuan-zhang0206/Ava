#!/usr/bin/env python
"""ava-root e2 drill: `ava start`/`ava stop` driven by the root supervisor (W1.2e-2).

The acceptance run for the root-driven start/stop path (task #3365): over one
worktree cluster (its own home, ports and data plane), it drives the real CLI
through both paths and compares their service face.

Phases:
  preflight  validate the worktree cluster home; record machine resources
  baseline   switch OFF: `ava start` -> status -> `ava stop` (the session
             path's service face, for the alignment comparison)
  start      switch ON (AVA_ROOT_DRIVER_ENABLED=1): `ava start` -> root alive,
             tree == the generation's own manifest, units running, health
             verdicts landed; `ava status` captured
  stop       `ava stop` -> tree down and the root process exited
  restart    `ava start` again (recovery), then once more on the running tree
             (idempotence: the root pid and every unit pid unchanged); a final
             `ava stop` leaves the cluster stopped

Environment discipline: the CLI runs with a minimal allowlisted env (no
ambient AVA_* — the agent process tree carries production values that must
never leak into a dev drill) + an explicit AVA_HOME for the worktree cluster.
OS-job registration is switched off wholesale for every phase
(`AVA_OS_JOBS_ENABLED=false`, the same gate the test suite uses): without it
the converge step re-registers the watchdog/health probes, whose 60 s cycles
would respawn session-form watchdogs into the root-owned tree and race this
drill (job retirement is W1.4). A bootout-only cleanup pass still runs after
the baseline start for leftovers — never `launchctl disable`, which poisons
every later converge with "bootstrap failed: service is disabled". The
trimmed roster is the #3230-approved set: {milvus, browser, browser-mcp,
computer-mcp, otel-collector} disabled on BOTH paths, so the comparison is
like-for-like.

Resource guardrail (macmini is the shared production host): a memory / load /
disk sample is taken before every start, a breach aborts the run and stops the
cluster. Evidence (CLI output, root status snapshots, manifests, status
snapshots, resource samples) is retained under <workdir>/evidence — never /tmp.

Exit 0 = every phase passed.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import cast

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from services.ava_root.client import RootClient, RootClientError  # noqa: E402

_TRIMMED_SERVICES = ("milvus", "browser", "browser-mcp", "computer-mcp", "otel-collector")
_JOB_SUFFIXES = ("watchdog-probe.agent-runner", "watchdog-probe.gateway", "health-probe")
_SOCKET_NAME = "ava-root.sock"
_START_TIMEOUT_S = 900.0
_STOP_TIMEOUT_S = 600.0
_HEALTH_WAIT_S = 150.0
_POLL_S = 2.0
_MIN_AVAILABLE_BYTES = 800 * 1024 * 1024
_MAX_LOAD_PER_CORE = 3.0


class _DrillError(RuntimeError):
    """A drill phase failed."""


# ─── process/env plumbing ───────────────────────────────────────────────────


def _minimal_env(home: Path, *, switch_on: bool) -> dict[str, str]:
    """The allowlisted child env — no ambient AVA_* leaks into the dev cluster."""
    env = {
        "PATH": f"{REPO_ROOT / '.venv' / 'bin'}:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        "HOME": str(Path.home()),
        "LANG": "en_US.UTF-8",
        "TMPDIR": tempfile.gettempdir(),  # the drill host's temp dir, passed through
        "AVA_HOME": str(home),
        # Keep this dev cluster out of the host's launchd job table: the
        # probe jobs would race the root tree (see the module docstring).
        "AVA_OS_JOBS_ENABLED": "false",
    }
    if switch_on:
        env["AVA_ROOT_DRIVER_ENABLED"] = "1"
    return env


def _run_ava(
    repo: Path, home: Path, args: list[str], *, switch_on: bool, timeout: float
) -> subprocess.CompletedProcess[str]:
    """Run this checkout's `ava` CLI against the drill cluster."""
    return subprocess.run(  # noqa: S603 — this checkout's own console script
        [str(repo / ".venv" / "bin" / "ava"), *args],
        cwd=repo,
        env=_minimal_env(home, switch_on=switch_on),
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
    switch_on: bool,
    timeout: float,
    evidence: Path,
    tag: str,
) -> str:
    """Run the CLI, save its output, and fail the drill on a non-zero exit."""
    result = _run_ava(repo, home, args, switch_on=switch_on, timeout=timeout)
    blob = f"$ ava {' '.join(args)}\nrc={result.returncode}\n\n--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}\n"
    (evidence / f"{tag}.out").write_text(blob, encoding="utf-8")
    if result.returncode != 0:
        raise _DrillError(f"`ava {' '.join(args)}` exited {result.returncode}; see {tag}.out")
    return result.stdout


def _root_client(home: Path) -> RootClient:
    return RootClient(home / "run" / "ava-root" / _SOCKET_NAME, timeout=5.0)


def _root_status(home: Path) -> dict[str, object] | None:
    """The root's status result, or None when no root answers."""
    try:
        response = _root_client(home).status()
    except RootClientError:
        return None
    result = response.get("result")
    return result if response.get("ok") and isinstance(result, dict) else None


def _units(status: dict[str, object] | None) -> dict[str, dict[str, object]]:
    rows = status.get("units") if status else None
    units: dict[str, dict[str, object]] = {}
    for row in rows if isinstance(rows, list) else []:
        if isinstance(row, dict) and isinstance(row.get("id"), str):
            units[str(row["id"])] = row
    return units


def _status_rows(output: str) -> dict[str, str]:
    """The service face as `{ava-<service>: "<session-mark><probe-mark>"}."""
    rows: dict[str, str] = {}
    pattern = re.compile(r"^(ava-[a-z0-9-]+)\s+(\S)\s+(\S) \(")
    for line in output.splitlines():
        found = pattern.match(line)
        if found:
            rows[found.group(1)] = found.group(2) + found.group(3)
    return rows


def _service_sessions(home: Path) -> list[str]:
    names = []
    for record in sorted((home / "run" / "sessions").glob("*.json")):
        names.append(record.stem)
    return names


# ─── resource guardrail ─────────────────────────────────────────────────────


def _resource_line() -> str:
    load = os.getloadavg()
    import psutil  # a project dependency; the drill is a dev-host script

    available = psutil.virtual_memory().available
    stat = os.statvfs(str(Path.home()))
    free_gb = stat.f_bavail * stat.f_frsize / 1024**3
    return (
        f"loadavg={load[0]:.2f}/{load[1]:.2f}/{load[2]:.2f} "
        f"available={available / 1024**3:.2f}GiB disk_free={free_gb:.1f}GiB"
    )


def _resource_guard(evidence: Path, tag: str) -> None:
    line = _resource_line()
    with (evidence / "resources.log").open("a", encoding="utf-8") as handle:
        handle.write(f"[{tag}] {line}\n")
    print(f"  resources[{tag}]: {line}")
    import psutil

    available = psutil.virtual_memory().available
    load1 = os.getloadavg()[0]
    cores = os.cpu_count() or 1
    if available < _MIN_AVAILABLE_BYTES:
        raise _DrillError(f"resource circuit breaker: available {available / 1024**3:.2f} GiB")
    if load1 > _MAX_LOAD_PER_CORE * cores:
        raise _DrillError(f"resource circuit breaker: load1 {load1:.1f} over {cores} core(s)")


# ─── the service-health face ────────────────────────────────────────────────


def _frontend_substance(units: dict[str, dict[str, object]], home: Path) -> tuple[bool, str]:
    """Verify the frontend the way its verdict cannot under the tree.

    The frontend's identity probe is session-record-bound (a W1.2e-2 inventory
    item): under the root tree it reads `port-taken` while the unit serves.
    Substance = unit process alive, app port answers 2xx, and every app-port
    listener is a descendant of the unit's process. The app port comes from the
    cluster home's own `.env` — the same source `converge` writes and the gate
    reads (`services.gate.helpers.app_port`), never the ambient environment.
    """
    import httpx
    import psutil
    from dotenv import dotenv_values

    unit = units.get("frontend")
    pid = unit.get("pid") if unit else None
    if unit is None or unit.get("state") != "running" or not isinstance(pid, int):
        return False, f"frontend unit not running (state={unit.get('state') if unit else 'absent'})"
    raw_port = dotenv_values(home / ".env").get("AVA_APP_PORT")
    app_port = int(raw_port) if raw_port else 0
    if app_port == 0:
        return False, f"no AVA_APP_PORT in {home / '.env'}"
    try:
        response = httpx.get(f"http://localhost:{app_port}", timeout=5.0)
        if response.status_code >= 400:
            return False, f"frontend answered {response.status_code}"
    except Exception as exc:
        return False, f"frontend port unreachable: {exc}"
    listeners: set[int] = set()
    for proc in psutil.process_iter():
        try:
            for conn in proc.net_connections(kind="tcp"):
                if conn.status == psutil.CONN_LISTEN and conn.laddr.port == app_port:
                    listeners.add(proc.pid)
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            continue
    if not listeners:
        return False, f"no listener on {app_port}"
    for listener in listeners:
        try:
            chain = {p.pid for p in psutil.Process(listener).parents()}
        except psutil.NoSuchProcess:
            return False, f"listener {listener} vanished mid-check"
        if pid not in chain and listener != pid:
            return False, f"listener {listener} is not a descendant of the frontend unit"
    return True, f"http 2xx on {app_port}; listener(s) {sorted(listeners)} under unit {pid}"


def _assert_services_healthy(units: dict[str, dict[str, object]], home: Path) -> list[str]:
    """Phase assertions over the root status; returns the notes it accepted."""
    notes: list[str] = []
    broken: list[str] = []
    for unit_id, unit in sorted(units.items()):
        state = unit.get("state")
        if state != "running":
            broken.append(f"{unit_id}: state={state} last_error={unit.get('last_error')}")
    if broken:
        raise _DrillError("units not running: " + "; ".join(broken))
    status = _root_status(home)
    health = (status or {}).get("health")
    health_units = health if isinstance(health, dict) else {}
    for unit_id in sorted(units):
        row = health_units.get(unit_id)
        verdict = row.get("last_verdict") if isinstance(row, dict) else None
        if unit_id == "frontend":
            ok, detail = _frontend_substance(units, home)
            if not ok:
                raise _DrillError(f"frontend substance check failed: {detail}")
            notes.append(
                f"frontend: verdict={verdict!r} (session-bound probe; known difference) — {detail}"
            )
            continue
        if row is None:
            notes.append(f"{unit_id}: no health entry (no probe surface)")
            continue
        if verdict != "alive":
            raise _DrillError(
                f"{unit_id}: health verdict {verdict!r} ({(row or {}).get('last_detail')})"
            )
        notes.append(f"{unit_id}: alive")
    return notes


def _wait_health_rounds(home: Path) -> None:
    """Wait (bounded) until every health-registered unit carries a verdict."""
    deadline = time.monotonic() + _HEALTH_WAIT_S
    while time.monotonic() < deadline:
        status = _root_status(home)
        health = (status or {}).get("health")
        if (
            isinstance(health, dict)
            and health
            and all(isinstance(row, dict) and row.get("last_verdict") for row in health.values())
        ):
            return
        time.sleep(_POLL_S)
    raise _DrillError(f"health verdicts did not land within {_HEALTH_WAIT_S:.0f}s")


# ─── phases ─────────────────────────────────────────────────────────────────


def _disable_args() -> list[str]:
    args: list[str] = []
    for service in _TRIMMED_SERVICES:
        args += ["--disable-service", service]
    return args


def _job_labels(home: Path) -> list[str]:
    """This cluster's probe-job labels: plists whose AVA_HOME is this home."""
    import plistlib

    labels: list[str] = []
    for suffix in _JOB_SUFFIXES:
        for plist in sorted(
            (Path.home() / "Library" / "LaunchAgents").glob(f"com.ava.*{suffix}.plist")
        ):
            try:
                with plist.open("rb") as handle:
                    data = plistlib.load(handle)
            except (OSError, plistlib.InvalidFileException):
                continue  # an unreadable plist is some other job's problem
            env = data.get("EnvironmentVariables") or {}
            if str(env.get("AVA_HOME", "")) == str(home):
                labels.append(str(data.get("Label", plist.stem)))
    return labels


def _unload_probe_jobs(home: Path, evidence: Path) -> None:
    """Boot out this cluster's probe jobs (retirement = W1.4; see the module docstring)."""
    uid = os.getuid()
    lines: list[str] = []
    for label in _job_labels(home):
        # bootout only: `launchctl disable` would make every later converge
        # fail ("bootstrap failed: service is disabled") — the first drill run
        # proved it the hard way.
        command = ["launchctl", "bootout", f"gui/{uid}/{label}"]
        result = subprocess.run(command, capture_output=True, text=True, check=False)  # noqa: S603
        lines.append(f"$ {' '.join(command)}\nrc={result.returncode} {result.stderr.strip()}")
    (evidence / "jobs-unloaded.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"  unloaded probe job(s): {', '.join(_job_labels(home)) or '(none)'}")


def _phase_preflight(args: argparse.Namespace, evidence: Path) -> Path:
    home = args.home
    if not home.is_dir() or not (home / ".env").is_file():
        raise _DrillError(
            f"{home} is not an installed cluster home — run `scripts/install.sh --worktree` first"
        )
    _resource_guard(evidence, "preflight")
    return home


def _phase_baseline(repo: Path, home: Path, evidence: Path) -> dict[str, str]:
    _resource_guard(evidence, "baseline")
    _ava_must(
        repo,
        home,
        ["start", *_disable_args()],
        switch_on=False,
        timeout=_START_TIMEOUT_S,
        evidence=evidence,
        tag="baseline-start",
    )
    status_out = _ava_must(
        repo,
        home,
        ["status"],
        switch_on=False,
        timeout=_STOP_TIMEOUT_S,
        evidence=evidence,
        tag="baseline-status",
    )
    face = _status_rows(status_out)
    print(f"  baseline service face: {len(face)} row(s)")
    _unload_probe_jobs(home, evidence)
    _ava_must(
        repo,
        home,
        ["stop", "--yes"],
        switch_on=False,
        timeout=_STOP_TIMEOUT_S,
        evidence=evidence,
        tag="baseline-stop",
    )
    return face


def _phase_start(
    repo: Path, home: Path, evidence: Path, tag: str
) -> tuple[dict[str, str], dict[str, int]]:
    _resource_guard(evidence, tag)
    _ava_must(
        repo,
        home,
        ["start", *_disable_args()],
        switch_on=True,
        timeout=_START_TIMEOUT_S,
        evidence=evidence,
        tag=f"{tag}-start",
    )
    status = _root_status(home)
    if status is None:
        raise _DrillError("no root answers on the control socket after start")
    (evidence / f"{tag}-root-status.json").write_text(
        json.dumps(status, indent=2) + "\n", encoding="utf-8"
    )
    units = _units(status)
    manifest_path = home / "run" / "ava-root" / "manifests.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest_ids = {str(unit["id"]) for unit in manifest["units"]}
    if set(units) != manifest_ids:
        raise _DrillError(
            f"tree != manifest: missing {sorted(manifest_ids - set(units))}, extra {sorted(set(units) - manifest_ids)}"
        )
    if manifest_ids & {"gateway-watchdog", "agent-runner-watchdog"}:
        raise _DrillError("watchdogs must not be tree units (W1.2a absorption)")
    failures_path = home / "last_launch_failures"
    if failures_path.exists():
        raise _DrillError(f"launch failures recorded: {failures_path.read_text().strip()}")
    _wait_health_rounds(home)
    status = _root_status(home)
    units = _units(status)
    notes = _assert_services_healthy(units, home)
    (evidence / f"{tag}-health-notes.txt").write_text("\n".join(notes) + "\n", encoding="utf-8")
    status_out = _ava_must(
        repo,
        home,
        ["status"],
        switch_on=True,
        timeout=_STOP_TIMEOUT_S,
        evidence=evidence,
        tag=f"{tag}-status",
    )
    pids: dict[str, int] = {}
    for unit_id, row in units.items():
        pid = row.get("pid")
        if isinstance(pid, int):
            pids[str(unit_id)] = pid
    root_row_raw = status.get("root") if status else None
    root_row = cast("dict[str, object]", root_row_raw) if isinstance(root_row_raw, dict) else None
    root_pid = root_row.get("pid") if root_row else None
    pids["__root__"] = root_pid if isinstance(root_pid, int) else -1
    return _status_rows(status_out), pids


def _assert_fresh_generation(previous: dict[str, int], fresh: dict[str, int]) -> None:
    """A re-start after a stop must be a new generation, not adopted pids."""
    if fresh == previous:
        raise _DrillError("a fresh start reused the previous generation's pids")


def _assert_idempotent_pids(before: dict[str, int], after: dict[str, int]) -> None:
    """A start over a running tree must not respawn anything."""
    if after != before:
        raise _DrillError(f"idempotent start changed pids: {before} -> {after}")


def _phase_stop(repo: Path, home: Path, evidence: Path, tag: str) -> None:
    _ava_must(
        repo,
        home,
        ["stop", "--yes"],
        switch_on=True,
        timeout=_STOP_TIMEOUT_S,
        evidence=evidence,
        tag=f"{tag}-stop",
    )
    if _root_status(home) is not None:
        raise _DrillError("the root still answers after stop")
    for session in _service_sessions(home):
        if session.startswith("ava-") and "watchdog" not in session:
            raise _DrillError(f"service session {session} still recorded after stop")
    _ava_must(
        repo,
        home,
        ["status"],
        switch_on=True,
        timeout=_STOP_TIMEOUT_S,
        evidence=evidence,
        tag=f"{tag}-status-after",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ava-root-e2-drill", description=__doc__)
    parser.add_argument(
        "--workdir", required=True, type=Path, help="evidence root (absolute, not /tmp)"
    )
    parser.add_argument("--home", required=True, type=Path, help="the worktree cluster home")
    parser.add_argument("--repo", type=Path, default=REPO_ROOT)
    parser.add_argument("--skip-baseline", action="store_true")
    args = parser.parse_args(argv)

    if not args.workdir.is_absolute():
        print("FAIL(args): workdir must be absolute", file=sys.stderr)
        return 1
    evidence = args.workdir / "evidence"
    evidence.mkdir(parents=True, exist_ok=True)
    args.home = args.home.expanduser()

    phases: list[tuple[str, str]] = []  # (phase, verdict)
    home = args.home
    try:
        _phase_preflight(args, evidence)
        phases.append(("preflight", "PASS"))
        baseline_face: dict[str, str] = {}
        if not args.skip_baseline:
            baseline_face = _phase_baseline(args.repo, home, evidence)
            phases.append(("baseline", "PASS"))
        start_face, start_pids = _phase_start(args.repo, home, evidence, "start")
        phases.append(("start", "PASS"))
        _phase_stop(args.repo, home, evidence, "stop")
        phases.append(("stop", "PASS"))
        _, restart_pids = _phase_start(args.repo, home, evidence, "restart")
        phases.append(("restart", "PASS"))
        _assert_fresh_generation(start_pids, restart_pids)
        _, idempotent_pids = _phase_start(args.repo, home, evidence, "idempotent")
        _assert_idempotent_pids(restart_pids, idempotent_pids)
        phases.append(("idempotent", "PASS"))
        _phase_stop(args.repo, home, evidence, "final")
        phases.append(("final-stop", "PASS"))
        alignment = [
            f"{name}: baseline={baseline_face.get(name)} root={start_face.get(name)}"
            for name in sorted(set(baseline_face) | set(start_face))
            if baseline_face.get(name) != start_face.get(name)
        ]
        (evidence / "alignment.txt").write_text("\n".join(alignment) + "\n", encoding="utf-8")
        if alignment:
            print("  alignment differences (baseline vs root):")
            for line in alignment:
                print(f"    {line}")
    except _DrillError as exc:
        phases.append(("failed", "FAIL"))
        print(f"FAIL: {exc}", file=sys.stderr)
        _resource_guard_silent(home, evidence)
        subprocess.run(  # noqa: S603 — best-effort stop of the drill cluster
            [str(args.repo / ".venv" / "bin" / "ava"), "stop", "--yes"],
            cwd=args.repo,
            env=_minimal_env(home, switch_on=True),
            capture_output=True,
            text=True,
            timeout=_STOP_TIMEOUT_S,
            check=False,
        )
        (evidence / "summary.txt").write_text(
            "\n".join(f"{name}: {verdict}" for name, verdict in phases) + "\n", encoding="utf-8"
        )
        return 1

    _resource_guard_silent(home, evidence)
    summary = "\n".join(f"{name}: {verdict}" for name, verdict in phases)
    (evidence / "summary.txt").write_text(summary + "\n", encoding="utf-8")
    print("\n" + summary)
    print(f"evidence: {evidence}")
    return 0


def _resource_guard_silent(home: Path, evidence: Path) -> None:
    """A best-effort final sample — a missing one must not fail the run."""
    del home
    from contextlib import suppress

    with suppress(Exception):
        line = _resource_line()
        with (evidence / "resources.log").open("a", encoding="utf-8") as handle:
            handle.write(f"[final] {line}\n")


if __name__ == "__main__":
    raise SystemExit(main())
