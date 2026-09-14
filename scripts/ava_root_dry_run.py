#!/usr/bin/env python
"""ava-root dev dry run: manifests + wiring over a throwaway tree (W1.2e).

The root-side counterpart of `scripts/two_section_chain_smoke.py`: where the
two-section smoke drives the OS-edge chain (launchd -> helper -> root), this
drill drives the ROOT's own new surfaces over a throwaway tree — the manifest
generator, the daemon's `--wiring` hook, the reference/drill assemblies, and
the G5 per-unit log layout — against a dev directory, never production.

Phases:
  generate   build the light manifest (validated by load_manifests) and, when
             `--capabilities` is passed, exercise the production generator
             into the evidence directory
  launch     start the daemon with the drill wiring hook and wait for ready
  status     verify the attach surfaces (health/metrics), a real probe round,
             and the per-unit log layout
  ops        client verbs on the live tree: down / up / restart
  revive     SIGSTOP the heartbeat unit (pid stays, beats stop) and let the
             root's own health path revive it: probe down -> restart ->
             verified alive
  stop       SIGTERM; the daemon must exit 0

Nothing here touches production: the run dir lives under --workdir
(/tmp/ava-root-dry-run by default), the units are sleepers, and the script
refuses a workdir under a protected home — the one exception is the agent
scratch tree `<home>/workspaces/`, so a drill's evidence can live in a worker
workspace. Evidence (manifests, status snapshots, logs) is retained under
<workdir>/evidence unless --cleanup is passed.

Exit 0 = every phase passed.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import NoReturn, cast

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from services.ava_root.client import RootClient, RootClientError  # noqa: E402
from services.ava_root.ipc import ResponsePayload  # noqa: E402
from services.ava_root.manifest import ManifestError, load_manifests  # noqa: E402
from services.ava_root_glue.manifests import generate  # noqa: E402
from shared.config import settings  # noqa: E402
from shared.proc import process_alive  # noqa: E402

_DEFAULT_WORKDIR = Path("/tmp/ava-root-dry-run")  # noqa: S108 - scratch evidence dir, never secret
_SOCKET_NAME = "ava-root.sock"
_WIRING = "services.ava_root_glue.drill:build_drill_wiring"
_REVIVE_UNIT = "light-beat"
_UNIT_IDS = ("light-a", "light-a-child", "light-b", _REVIVE_UNIT)
_READY_TIMEOUT_S = 30.0
_ROUND_TIMEOUT_S = 45.0
_REVIVE_DEADLINE_S = 120.0
_POLL_S = 0.25


def _fail(phase: str, detail: str) -> NoReturn:
    print(f"FAIL(phase={phase}): {detail}")
    raise SystemExit(1)


def _phase_pass(phase: str, detail: str) -> None:
    print(f"PASS(phase={phase}): {detail}")


def _unit_exec(python: str, unit_id: str) -> list[str]:
    script = f"import time; print({unit_id!r} + ' ready', flush=True); time.sleep(3600)"
    return [python, "-u", "-c", script]


def _beat_exec(python: str, heartbeat_path: Path) -> list[str]:
    """A unit that touches `heartbeat_path` every second (the revive drill's surface)."""
    script = (
        "import time\n"
        "from pathlib import Path\n"
        f"beat = Path({str(heartbeat_path)!r})\n"
        "beat.parent.mkdir(parents=True, exist_ok=True)\n"
        f"print('{_REVIVE_UNIT} ready', flush=True)\n"
        "while True:\n"
        "    beat.touch()\n"
        "    time.sleep(1)\n"
    )
    return [python, "-u", "-c", script]


def _light_manifest(python: str, *, heartbeat_path: Path) -> dict[str, object]:
    return {
        "units": [
            {"id": "light-a", "exec": _unit_exec(python, "light-a"), "restart": "always"},
            {
                "id": "light-a-child",
                "exec": _unit_exec(python, "light-a-child"),
                "restart": "always",
                "attach": "light-a",
            },
            {"id": "light-b", "exec": _unit_exec(python, "light-b"), "restart": "always"},
            {"id": _REVIVE_UNIT, "exec": _beat_exec(python, heartbeat_path), "restart": "always"},
        ]
    }


def _status(client: RootClient) -> dict[str, object]:
    response = client.status()
    if not response.get("ok"):
        _fail("status", f"root answered not-ok: {response}")
    return cast("dict[str, object]", response.get("result"))


def _ok_call(phase: str, response: ResponsePayload, what: str) -> None:
    if not response.get("ok"):
        _fail(phase, f"{what} failed: {response}")


def _units_by_id(status: dict[str, object]) -> dict[str, dict[str, object]]:
    units = cast("list[dict[str, object]]", status["units"])
    return {cast("str", unit["id"]): unit for unit in units}


def _pid_of(status: dict[str, object], unit_id: str) -> int | None:
    unit = _units_by_id(status)[unit_id]
    pid = unit.get("pid")
    return pid if isinstance(pid, int) else None


def _beat_mtime(path: Path) -> float | None:
    """The heartbeat file's last-touch wall time; None when the file is absent."""
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def _wait_ready(run_dir: Path, proc: subprocess.Popen[bytes], log_path: Path) -> RootClient:
    client = RootClient(run_dir / _SOCKET_NAME, timeout=5.0)
    deadline = time.monotonic() + _READY_TIMEOUT_S
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            _fail("launch", f"daemon exited early with {proc.returncode}; log:\n{_tail(log_path)}")
        if (run_dir / _SOCKET_NAME).exists():
            try:
                if client.status().get("ok"):
                    return client
            except RootClientError:
                pass
        time.sleep(_POLL_S)
    _fail("launch", f"daemon did not become ready; log:\n{_tail(log_path)}")


def _wait_rounds(client: RootClient, *, timeout: float) -> dict[str, object]:
    """Wait until a health round and a self-check round have both landed."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = _status(client)
        health = status.get("health")
        metrics = status.get("metrics")
        if isinstance(health, dict) and health and isinstance(metrics, dict):
            chain = metrics.get("chain")
            if isinstance(chain, dict) and cast("int", chain.get("rounds", 0)) >= 1:
                return status
        time.sleep(_POLL_S)
    return _status(client)


def _tail(path: Path, limit: int = 3000) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[-limit:]
    except OSError:
        return "(no log)"


def _save(evidence: Path, name: str, payload: object) -> None:
    path = evidence / name
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def main() -> int:  # noqa: PLR0915 - one bounded drill lifecycle: every phase, wait, and teardown live together on purpose
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument(
        "--workdir",
        default=str(_DEFAULT_WORKDIR),
        help="throwaway directory for the drill (default: /tmp/ava-root-dry-run)",
    )
    parser.add_argument(
        "--python", default=sys.executable, help="interpreter for the daemon + units"
    )
    parser.add_argument(
        "--capabilities",
        default=None,
        help=(
            "capability set for the production-generator sample (e.g. gateway,agent-runner);"
            " omit to skip the sample"
        ),
    )
    parser.add_argument("--cleanup", action="store_true", help="remove the workdir at the end")
    args = parser.parse_args()

    workdir = Path(args.workdir).expanduser()
    if not workdir.is_absolute():
        _fail("args", f"workdir must be absolute: {workdir}")
    protected = {Path.home() / ".ava", Path(settings.general.ava_home).expanduser()}
    for base in protected:
        under_home = workdir == base or base in workdir.parents
        # The one carve-out is the agent scratch tree: a root-side drill is
        # expected to retain its evidence in a worker's workspace.
        in_scratch = (base / "workspaces") in workdir.parents
        if under_home and not in_scratch:
            _fail("args", f"refusing a workdir under {base}: {workdir} is for dev drills only")

    workdir.mkdir(parents=True, exist_ok=True)
    evidence = workdir / "evidence"
    evidence.mkdir(parents=True, exist_ok=True)
    run_dir = workdir / "root-run"
    manifests_path = workdir / "manifests.json"
    daemon_log = workdir / "root.log"

    # -- generate ---------------------------------------------------------
    heartbeat_path = run_dir / "heartbeat" / f"{_REVIVE_UNIT}.beat"
    light = _light_manifest(args.python, heartbeat_path=heartbeat_path)
    manifests_path.write_text(json.dumps(light, indent=2) + "\n", encoding="utf-8")
    load_manifests(manifests_path)  # the daemon's own reader must accept it
    _save(evidence, "manifests.light.json", light)
    production_note = "skipped (no --capabilities given)"
    if args.capabilities is not None:
        caps = [p.strip() for p in args.capabilities.split(",") if p.strip()]
        try:
            prod_path = generate(evidence / "manifests.production.json", capabilities=caps)
            prod_units = load_manifests(prod_path).units
            production_note = f"{len(prod_units)} unit(s) -> {prod_path.name}"
        except ManifestError as exc:
            _fail("generate", f"production manifest generation failed: {exc}")
    light_units = cast("list[object]", light["units"])
    _phase_pass(
        "generate",
        f"light manifest {len(light_units)} units; production generator: {production_note}",
    )

    # -- launch -----------------------------------------------------------
    command = [
        args.python,
        "-m",
        "services.ava_root",
        "--run-dir",
        str(run_dir),
        "--manifests",
        str(manifests_path),
        "--wiring",
        _WIRING,
    ]
    with daemon_log.open("wb") as log_file:
        proc = subprocess.Popen(  # noqa: S603 - fixed argv, this drill's own children
            command, cwd=REPO_ROOT, stdout=log_file, stderr=subprocess.STDOUT
        )
    try:
        client = _wait_ready(run_dir, proc, daemon_log)
        log_text = _tail(daemon_log)
        if "wired participant(s)" not in log_text:
            _fail("launch", f"ready line does not show wired participants; log:\n{log_text}")
        _phase_pass("launch", f"socket ready; {log_text.strip().splitlines()[-1]}")

        # -- status -------------------------------------------------------
        status = _status(client)
        units = _units_by_id(status)
        for unit_id in _UNIT_IDS:
            unit = units.get(unit_id)
            if unit is None or unit.get("state") != "running":
                _fail("status", f"unit {unit_id} not running: {unit}")
        for unit_id in _UNIT_IDS:
            log_path = run_dir / "logs" / unit_id / "output.log"
            if not log_path.exists():
                _fail("status", f"missing per-unit log {log_path}")
            text = log_path.read_text(encoding="utf-8", errors="replace")
            if f"{unit_id} ready" not in text:
                _fail("status", f"unit log {log_path} lacks its own marker")
            for other in _UNIT_IDS:
                if other != unit_id and f"{other} ready" in text:
                    _fail("status", f"unit log {log_path} leaked {other} output")
        drilled = _wait_rounds(client, timeout=_ROUND_TIMEOUT_S)
        health = cast("dict[str, object]", drilled["health"])
        for unit_id in _UNIT_IDS:
            verdict = cast("dict[str, object]", health.get(unit_id, {})).get("last_verdict")
            if verdict != "alive":
                _fail("status", f"health verdict for {unit_id} is {verdict!r}: {drilled}")
        metrics = cast("dict[str, object]", drilled["metrics"])
        _save(evidence, "status.with-rounds.json", drilled)
        _phase_pass(
            "status",
            f"health+metrics attached; rounds={cast('dict[str, object]', metrics['chain'])['rounds']};"
            " per-unit logs isolated",
        )

        # -- ops ----------------------------------------------------------
        _ok_call("ops", client.down("light-a"), "down light-a")
        after_down = _status(client)
        units_down = _units_by_id(after_down)
        for unit_id in ("light-a", "light-a-child"):
            if units_down[unit_id].get("state") != "stopped":
                _fail("ops", f"down light-a left {unit_id} {units_down[unit_id].get('state')}")
        _ok_call("ops", client.up("light-a"), "up light-a")
        after_up = _status(client)
        pid_before_restart = _pid_of(after_up, "light-a")
        units_up = _units_by_id(after_up)
        for unit_id in ("light-a", "light-a-child"):
            if units_up[unit_id].get("state") != "running":
                _fail("ops", f"up light-a left {unit_id} {units_up[unit_id].get('state')}")
        _ok_call("ops", client.restart("light-a"), "restart light-a")
        after_restart = _status(client)
        pid_after_restart = _pid_of(after_restart, "light-a")
        if pid_before_restart is None or pid_after_restart == pid_before_restart:
            _fail(
                "ops",
                f"restart did not replace the generation: {pid_before_restart} -> {pid_after_restart}",
            )
        _save(evidence, "status.after-ops.json", after_restart)
        _phase_pass(
            "ops",
            f"down/up subtree (light-a + child) ok; restart replaced pid {pid_before_restart} -> {pid_after_restart}",
        )

        # -- revive -------------------------------------------------------
        # The probe-driven revival: wedge the heartbeat unit (SIGSTOP — pid
        # alive, beats stopped), let the ROOT's own health path judge it down
        # and replace the generation, then confirm alive. No watchdog,
        # healthchecks or session process takes part.
        beat = run_dir / "heartbeat" / f"{_REVIVE_UNIT}.beat"
        before = _status(client)
        unit_before = _units_by_id(before)[_REVIVE_UNIT]
        pid_before = _pid_of(before, _REVIVE_UNIT)
        restarts_before = cast("int", unit_before.get("restart_count", -1))
        beat_before = _beat_mtime(beat)
        if pid_before is None or beat_before is None or time.time() - beat_before > 5.0:
            _fail(
                "revive",
                f"{_REVIVE_UNIT} not wedge-ready: pid={pid_before} beat_mtime={beat_before}",
            )
        try:
            os.kill(pid_before, signal.SIGSTOP)
        except OSError as exc:
            _fail("revive", f"cannot SIGSTOP pid {pid_before}: {exc}")
        wedged_at = time.time()
        _save(
            evidence,
            "revive.wedged.json",
            {
                "unit": _REVIVE_UNIT,
                "pid": pid_before,
                "restart_count": restarts_before,
                "status": before,
            },
        )

        deadline = time.monotonic() + _REVIVE_DEADLINE_S
        down_snapshot: dict[str, object] | None = None
        revived: dict[str, object] = {}
        while time.monotonic() < deadline:
            snapshot = _status(client)
            unit = _units_by_id(snapshot)[_REVIVE_UNIT]
            health = cast("dict[str, object]", snapshot.get("health") or {})
            unit_health = cast("dict[str, object]", health.get(_REVIVE_UNIT) or {})
            verdict = unit_health.get("last_verdict")
            if verdict == "down" and down_snapshot is None:
                down_snapshot = snapshot
            pid_now = unit.get("pid")
            beat_now = _beat_mtime(beat)
            if (
                down_snapshot is not None
                and unit.get("state") == "running"
                and isinstance(pid_now, int)
                and pid_now != pid_before
                and verdict == "alive"
                and beat_now is not None
                and beat_now > wedged_at
                and not process_alive(pid_before)
            ):
                revived = snapshot
                break
            time.sleep(_POLL_S)
        if not revived:
            _fail(
                "revive",
                f"no probe-driven revive within {_REVIVE_DEADLINE_S:.0f}s"
                f" (down_seen={down_snapshot is not None}); log:\n{_tail(daemon_log)}",
            )
        if down_snapshot is not None:
            _save(evidence, "revive.down.json", down_snapshot)

        unit_after = _units_by_id(revived)[_REVIVE_UNIT]
        pid_after = _pid_of(revived, _REVIVE_UNIT)
        restarts_after = cast("int", unit_after.get("restart_count", -1))
        if pid_after is None or restarts_after != restarts_before + 1:
            _fail(
                "revive",
                f"restart bookkeeping off: pid {pid_before} -> {pid_after},"
                f" restart_count {restarts_before} -> {restarts_after}",
            )

        log_text = _tail(daemon_log, limit=200_000)
        for marker in (
            f"unit {_REVIVE_UNIT}: down (pid {pid_before} alive but heartbeat stale",
            f"unit {_REVIVE_UNIT} started (pid {pid_after}",
            f"unit {_REVIVE_UNIT}: restarted, verified alive",
        ):
            if marker not in log_text:
                _fail("revive", f"daemon log lacks {marker!r};\n{_tail(daemon_log)}")
        revive_lines = [line for line in log_text.splitlines() if _REVIVE_UNIT in line]
        (evidence / "revive.daemon-lines.txt").write_text(
            "\n".join(revive_lines) + "\n", encoding="utf-8"
        )
        _save(evidence, "revive.after.json", revived)
        _save(
            evidence,
            "revive.json",
            {
                "unit": _REVIVE_UNIT,
                "wedge": f"SIGSTOP pid {pid_before}",
                "pid_before": pid_before,
                "pid_after": pid_after,
                "restart_count_before": restarts_before,
                "restart_count_after": restarts_after,
                "verdict_sequence": "alive -> down -> generation replaced -> verified alive",
                "old_pid_dead": not process_alive(pid_before),
                "old_gen_stop": (
                    "escalated to SIGKILL after the stop timeout"
                    if f"unit {_REVIVE_UNIT} did not stop within" in log_text
                    else "terminated within the stop window (no escalation needed)"
                ),
                "watchdog_participation": (
                    "none: revival came from the root's own HealthMonitor -> Supervisor.restart;"
                    " the drill rig runs sleepers only (no watchdog/healthchecks/sessions)"
                ),
            },
        )
        _phase_pass(
            "revive",
            f"{_REVIVE_UNIT} SIGSTOP wedge -> probe down -> generation replaced"
            f" (pid {pid_before} -> {pid_after}, restart_count {restarts_before} -> {restarts_after})"
            " -> verified alive",
        )
    finally:
        if proc.poll() is None:
            proc.send_signal(signal.SIGTERM)

    # -- stop -------------------------------------------------------------
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)
        _fail("stop", f"daemon ignored SIGTERM; log:\n{_tail(daemon_log)}")
    if proc.returncode != 0:
        _fail("stop", f"daemon exited {proc.returncode}; log:\n{_tail(daemon_log)}")
    _save(evidence, "daemon.log", _tail(daemon_log, limit=100000))
    _phase_pass("stop", "daemon exited 0 after SIGTERM")

    print(f"workdir: {workdir}")
    if args.cleanup:
        shutil.rmtree(workdir, ignore_errors=True)
        print("workdir removed (--cleanup)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
