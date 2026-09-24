"""The root-driven service path for `ava start` / `ava stop` (W1.2e-2).

`ava start` launches services as named sessions by default. When the
`services.root_driver_enabled` switch is on for a host, the service tree is
instead owned by the ava-root supervisor: start generates this cluster's K2
unit manifest, makes sure one root daemon runs it, and stop stops the tree
through that same daemon. The roster is the same either way — this module only
changes who launches the units (and therefore what "already running" means).

Branches at start:
- helper: `permissions_helper_spawn` is on and the helper answers — seed the
  root keeper (`root_seed`) with the daemon argv and wait for `running`. A
  committed helper that cannot be reached refuses loudly: falling back to a
  direct spawn would silently change process attribution, the identity
  commitment `shared.session_backend` states for every spawn face.
- direct: spawn `python -m services.ava_root` detached (own session, logs under
  the run dir) and wait for its control socket — or adopt a root already up.

An already-running root is reconciled, never duplicated: units this start
wants that are down are brought up through K1 (`up`, idempotent); on an
operator start (`persist_services`) tree units missing from this start's
roster are brought down (`down` — that is what `--disable-service` means
here); a desired unit the running tree does not know replaces the root
generation (manifests change only with the generation). Same roster + every
unit up is a pure status verification — the idempotent start.

Readiness judges `status()`: a unit is ready when it is running and its latest
health verdict is not `down` / `port-taken`; a unit with no verdict yet (the
health runner's first round lands on its interval) passes, the same way a
probe-less service never gated the session path. The wait is otherwise the
same tiered contract as `cli.commands._probe`: critical services keep the
whole bound, non-critical services get the short window and can never fail a
start. Identity probes read the target unit's own management mode (task
#3370): the frontend's reads the tree unit's pid on a root-driven host, not
the absent session record, so it no longer reads `port-taken` while the unit
serves. One difference from the session path remains deliberate and
inventoried in the W1.2e-2 PR: the verdict is the root's own health surface
(rounds land on the health interval, not on a fresh probe).

Stop maps `--keep-infra` (infrastructure lives outside the tree — unchanged)
and every preserved service (pause's browser, `--keep-service`) to a selective
`down`; with nothing preserved the whole tree stop also ends the root. A
helper-wired root is stopped through the keeper (`root_stop`), because only a
keeper-requested stop is not followed by a restart; a directly spawned root
takes SIGTERM. A stop that cannot reach the keeper refuses — a direct signal
would be a stop the keeper quietly undoes.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path
from typing import Any, cast

from cli.commands._probe import ReadinessWait
from cli.commands._repo import ServiceSpec, session_name
from cli.commands._session_lifecycle import LaunchOutcome
from shared.machine import MachineRoles

_ROOT_SOCKET_NAME = "ava-root.sock"
_ROOT_STDOUT_LOG = "root.stdout.log"
_ROOT_STDERR_LOG = "root.stderr.log"
_WIRING_REF = "services.ava_root_glue.glue:build_wiring"

# The bound for a freshly launched root to bind its control socket. The daemon
# spawns every unit before it serves, and a unit spawn is a fork+exec — the
# bound is spent only by a root that is alive and has bound nothing.
_ROOT_READY_TIMEOUT_S = 30.0
# The bound for a replaced/stopped root to finish tearing its tree down. The
# daemon's per-unit polite-stop window is 10 s, units stop in stop order, and
# an idle dev tree answers on the first poll — this is the deadline, not the
# expected cost.
_ROOT_STOP_TIMEOUT_S = 90.0
# Same cadence as the session readiness wait (`cli.commands._probe`).
_READY_POLL_INTERVAL_S = 0.5
_poll_sleep = time.sleep  # a named seam tests can patch (the _probe pattern)


class _RootDriverError(RuntimeError):
    """The root-driven tree could not be brought up, reconciled or stopped."""


# ─── switch + roster ────────────────────────────────────────────────────────


def _root_driven_enabled() -> bool:
    """Whether this host routes start/stop through the root supervisor.

    Thin alias of the one shared definition (`shared.root_driver`) — kept under
    this name because the CLI resolves it (and tests patch it) through the
    package namespace.
    """
    from shared.root_driver import root_drive_enabled

    return root_drive_enabled()


def _root_tree_roster(roles: MachineRoles, launch_skip: set[str]) -> tuple[ServiceSpec, ...]:
    """The units the root-driven start will run: the launch roster minus absorbed watchdogs.

    The watchdog sessions are absorbed into the root's own health path (W1.2a),
    so they are not units of the tree; everything else — config/capability
    gates and `--disable-service` included — narrows exactly as it does on the
    session path (`_launch_roster`).
    """
    from cli.commands._session_lifecycle import _launch_roster
    from services.ava_root_glue.manifests import ABSORBED_WATCHDOGS

    return tuple(
        spec
        for spec in _launch_roster(roles, launch_skip)
        if spec.session not in ABSORBED_WATCHDOGS
    )


# ─── transport helpers ──────────────────────────────────────────────────────


def _root_client(*, timeout: float = 5.0) -> Any:
    """A blocking client bound to this cluster's root control socket."""
    from services.ava_root.client import RootClient
    from shared.paths import root_run_dir

    return RootClient(root_run_dir() / _ROOT_SOCKET_NAME, timeout=timeout)


def _root_status(client: Any) -> dict[str, Any] | None:
    """The daemon's status result, or None when no root answers (yet)."""
    from services.ava_root.client import RootClientError

    try:
        response = client.status()
    except RootClientError:
        return None
    result = response.get("result")
    if response.get("ok") and isinstance(result, dict):
        return cast("dict[str, Any]", result)
    return None


def _root_units(status: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Unit rows by unit id from one status snapshot."""
    rows_raw = status.get("units")
    rows = cast("list[object]", rows_raw) if isinstance(rows_raw, list) else []
    units: dict[str, dict[str, Any]] = {}
    for row_raw in rows:
        if not isinstance(row_raw, dict):
            continue
        row = cast("dict[str, object]", row_raw)
        unit_id = row.get("id")
        if isinstance(unit_id, str):
            units[unit_id] = cast("dict[str, Any]", row)
    return units


def _call_ok(response: dict[str, Any], what: str) -> None:
    """Raise `_RootDriverError` when a K1 reply is not ok."""
    if not response.get("ok"):
        raise _RootDriverError(f"{what} failed: {response.get('code')}: {response.get('error')}")


def _helper_spawn_committed() -> bool:
    """Whether process creation on this host is committed to the permission helper."""
    from shared.session_backend import helper_spawn_enabled

    return helper_spawn_enabled()


def _helper_wire_ok() -> bool:
    """Whether the permission helper answers on this cluster's socket."""
    from services.permissions_helper import client as helper_client

    try:
        helper_client.ping()
        return True
    except Exception:
        return False


# ─── the start leg ──────────────────────────────────────────────────────────


def _root_argv(run_dir: Path, manifests: Path) -> list[str]:
    """The daemon command line: the K3 launch face the root package freezes."""
    return [
        sys.executable,
        "-m",
        "services.ava_root",
        "--run-dir",
        str(run_dir),
        "--manifests",
        str(manifests),
        "--wiring",
        _WIRING_REF,
    ]


def _root_child_env() -> dict[str, str]:
    """The root env, including the proof it may pass only to agent-host."""
    from shared.env_registry import manifest_certification_secret_env
    from shared.session_env import forward_env_dict

    return forward_env_dict() | manifest_certification_secret_env()


def _write_tree_manifests(
    roster: tuple[ServiceSpec, ...], repo: Path, *, roles: MachineRoles
) -> Path:
    """Generate and validate this cluster's K2 manifest (`$AVA_HOME/run/ava-root/manifests.json`)."""
    from services.ava_root_glue.manifests import generate
    from shared.paths import root_manifests_path

    manifests = root_manifests_path()
    generate(manifests, capabilities=sorted(roles), repo_root=repo, specs=roster)
    return manifests


def _log_tail(path: Path, lines: int = 20) -> str:
    """The last `lines` of a log file, for an actionable bring-up failure."""
    try:
        content = path.read_text(errors="replace").splitlines()
    except OSError:
        return "(no log)"
    return "\n".join(content[-lines:]) or "(empty log)"


def _spawn_direct(run_dir: Path, repo: Path, manifests: Path) -> subprocess.Popen[bytes]:
    """Launch the root daemon detached (own session), logging under the run dir."""
    stdout = (run_dir / _ROOT_STDOUT_LOG).open("ab")
    stderr = (run_dir / _ROOT_STDERR_LOG).open("ab")
    try:
        return subprocess.Popen(
            _root_argv(run_dir, manifests),
            cwd=repo,
            env=_root_child_env(),
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            start_new_session=True,
            close_fds=True,
        )
    finally:
        stdout.close()
        stderr.close()


def _await_root_status(
    client: Any,
    run_dir: Path,
    *,
    proc: subprocess.Popen[bytes] | None = None,
    timeout_s: float = _ROOT_READY_TIMEOUT_S,
) -> dict[str, Any]:
    """Poll the control socket until the daemon serves, or fail with its log tail."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if proc is not None and proc.poll() is not None:
            tail = _log_tail(run_dir / _ROOT_STDERR_LOG)
            raise _RootDriverError(
                f"ava-root exited before serving (rc={proc.returncode}); log tail:\n{tail}"
            )
        status = _root_status(client)
        if status is not None:
            return status
        _poll_sleep(_READY_POLL_INTERVAL_S)
    tail = _log_tail(run_dir / _ROOT_STDERR_LOG)
    raise _RootDriverError(
        f"ava-root did not become ready within {timeout_s:.0f}s; log tail:\n{tail}"
    )


def _seed_via_helper(run_dir: Path, repo: Path, manifests: Path) -> None:
    """Seed the root keeper and wait for it to report the root `running`."""
    from services.permissions_helper import client as helper_client

    try:
        wire = helper_client.seed_root(
            {
                "argv": _root_argv(run_dir, manifests),
                "cwd": str(repo),
                "run_dir": str(run_dir),
                "stdout": str(run_dir / _ROOT_STDOUT_LOG),
                "stderr": str(run_dir / _ROOT_STDERR_LOG),
                "env": _root_child_env(),
            }
        )
    except Exception as exc:
        raise _RootDriverError(f"root_seed over the helper failed: {exc}") from exc
    deadline = time.monotonic() + _ROOT_READY_TIMEOUT_S
    while time.monotonic() < deadline:
        state = wire.get("state")
        if state == "running":
            return
        if state == "conflict":
            raise _RootDriverError(
                f"another ava-root owns {run_dir}; the keeper rests in conflict and will not "
                "spawn (resolve the conflict first)"
            )
        seed_error = wire.get("seed_error")
        if seed_error:
            raise _RootDriverError(f"the root keeper rejected the seed: {seed_error}")
        _poll_sleep(_READY_POLL_INTERVAL_S)
        try:
            wire = helper_client.root_status()
        except Exception as exc:
            raise _RootDriverError(f"root_status over the helper failed: {exc}") from exc
    raise _RootDriverError(
        f"the helper did not bring ava-root up within {_ROOT_READY_TIMEOUT_S:.0f}s "
        f"(keeper state={wire.get('state')})"
    )


def _bring_up_root(run_dir: Path, repo: Path, manifests: Path, client: Any) -> dict[str, Any]:
    """Start a root — through the helper when committed, a direct spawn otherwise."""
    if _helper_spawn_committed():
        if not _helper_wire_ok():
            raise _RootDriverError(
                "permissions_helper_spawn is on but the helper is unreachable — refusing to "
                "fall back to a direct spawn: the helper is this host's spawn-identity "
                "commitment (bring the helper up, or clear permissions_helper_spawn)"
            )
        _seed_via_helper(run_dir, repo, manifests)
        return _await_root_status(client, run_dir)
    proc = _spawn_direct(run_dir, repo, manifests)
    print(f"  + ava-root spawned directly (pid {proc.pid})")
    return _await_root_status(client, run_dir, proc=proc)


def _missing_units(roster: tuple[ServiceSpec, ...], status: dict[str, Any]) -> set[str]:
    """Desired unit ids the running tree does not know."""
    units = _root_units(status)
    return {spec.session for spec in roster if spec.session not in units}


def _stop_root_process(
    run_dir: Path, client: Any, status: dict[str, Any], *, timeout_s: float
) -> None:
    """Stop the whole tree and wait for the root process to exit.

    A helper-seeded root is stopped through the keeper (`root_stop`): only a
    keeper-requested stop is not followed by a restart, so reaching for the
    wire when the keeper is down would be a stop the keeper undoes. A directly
    spawned root takes SIGTERM.
    """
    import os
    import signal

    from shared.proc import process_alive

    root = status.get("root")
    pid = cast("dict[str, Any]", root).get("pid") if isinstance(root, dict) else None
    if _helper_spawn_committed():
        if not _helper_wire_ok():
            raise _RootDriverError(
                "permissions_helper_spawn is on but the helper is unreachable — refusing to "
                "signal a helper-seeded root directly (the keeper would restart it); stop it "
                "through the helper"
            )
        from services.permissions_helper import client as helper_client

        try:
            helper_client.stop_root()
        except Exception as exc:
            raise _RootDriverError(f"root_stop over the helper failed: {exc}") from exc
    elif isinstance(pid, int) and process_alive(pid):
        os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        alive = isinstance(pid, int) and process_alive(pid)
        if not alive and _root_status(client) is None:
            return
        _poll_sleep(_READY_POLL_INTERVAL_S)
    raise _RootDriverError(f"ava-root at {run_dir} did not stop within {timeout_s:.0f}s")


def _reconcile_units(
    roster: tuple[ServiceSpec, ...], client: Any, status: dict[str, Any], *, down_extras: bool
) -> dict[str, Any]:
    """Bring desired units up (and, on an operator start, stale units down)."""
    units = _root_units(status)
    for spec in roster:
        unit = units.get(spec.session)
        if unit is None:
            continue  # handled by the generation-replace path; classified below
        if unit.get("state") != "running" or unit.get("desired") != "running":
            reason = unit.get("last_error") or unit.get("last_exit") or unit.get("state")
            print(f"  ↑ ava-root unit {session_name(spec.session)} ({reason}) — bringing it up")
            _call_ok(client.up(spec.session), f"up {spec.session}")
    if down_extras:
        desired = {spec.session for spec in roster}
        for unit_id in sorted(units):
            if unit_id in desired or units[unit_id].get("desired") == "stopped":
                continue
            print(
                f"  ↓ ava-root unit {session_name(unit_id)} is not in this start's roster "
                "— bringing it down"
            )
            _call_ok(client.down(unit_id), f"down {unit_id}")
    refreshed = _root_status(client)
    return refreshed if refreshed is not None else status


def _classify_units(roster: tuple[ServiceSpec, ...], status: dict[str, Any]) -> LaunchOutcome:
    """The launch verdict: units that should run but could not be spawned."""
    units = _root_units(status)
    failed: list[str] = []
    for spec in roster:
        name = session_name(spec.session)
        unit = units.get(spec.session)
        if unit is None:
            print(f"  ✗ {name}: missing from the ava-root tree", file=sys.stderr)
            failed.append(name)
            continue
        if unit.get("state") != "running" and unit.get("last_error"):
            print(f"  ✗ {name}: {unit.get('last_error')}", file=sys.stderr)
            failed.append(name)
    return LaunchOutcome(roster, tuple(failed))


def _ensure_root_service_tree(
    roster: tuple[ServiceSpec, ...], repo: Path, *, roles: MachineRoles, reconcile: bool
) -> LaunchOutcome:
    """Ensure the root-owned tree runs exactly this start's roster.

    `reconcile` (the operator/`persist_services` flag) authorizes bringing
    stale tree units down; an internal restart only brings its units up and
    leaves them otherwise alone. Either way nothing is respawned while it is
    already running: the same roster with every unit up is a status check.
    """
    from shared.paths import root_run_dir

    run_dir = root_run_dir()
    manifests = _write_tree_manifests(roster, repo, roles=roles)
    client = _root_client()
    try:
        status = _root_status(client)
        if status is not None:
            missing = _missing_units(roster, status)
            if missing:
                print(
                    "  ⚠ ava-root's generation predates this roster (missing: "
                    + ", ".join(sorted(missing))
                    + ") — replacing the root generation"
                )
                _stop_root_process(run_dir, client, status, timeout_s=_ROOT_STOP_TIMEOUT_S)
                status = None
        if status is None:
            status = _bring_up_root(run_dir, repo, manifests, client)
        status = _reconcile_units(roster, client, status, down_extras=reconcile)
    except _RootDriverError as exc:
        print(f"  ✗ ava-root tree bring-up failed: {exc}", file=sys.stderr)
        return LaunchOutcome(roster, tuple(session_name(spec.session) for spec in roster))
    return _classify_units(roster, status)


# ─── the start-body facades (one call site each) ────────────────────────────
#
# `_cmd_start_body` picks its path through these three, so the body carries one
# line per leg instead of a root/session branch per leg. Every hop goes through
# the `cli.commands` namespace, the same seam the session path's callers and
# tests already patch.


def _start_roster(
    roles: MachineRoles, launch_skip: set[str]
) -> tuple[bool, tuple[ServiceSpec, ...]]:
    """Read the switch and return `(root_driven, this start's roster)`."""
    import cli.commands as _ns

    root_driven = _ns._root_driven_enabled()
    if root_driven:
        return root_driven, _ns._root_tree_roster(roles, launch_skip)
    return root_driven, _ns._launch_roster(roles, launch_skip)


def _launch_service_tree(
    root_driven: bool,  # noqa: FBT001 — path selector, always the caller's own value
    roster: tuple[ServiceSpec, ...],
    repo: Path,
    roles: MachineRoles,
    launch_skip: set[str],
    *,
    reconcile: bool,
) -> LaunchOutcome:
    """Launch this start's roster: the root-owned tree, or the named sessions."""
    import cli.commands as _ns

    if root_driven:
        return _ns._ensure_root_service_tree(roster, repo, roles=roles, reconcile=reconcile)
    return _ns._launch_sessions(roles, launch_skip, repo)


def _wait_for_service_tree(
    root_driven: bool,  # noqa: FBT001 — path selector, always the caller's own value
    roster: tuple[ServiceSpec, ...],
    *,
    timeout_s: float,
) -> ReadinessWait:
    """Wait for this start's roster: the root status surface, or the probes."""
    import cli.commands as _ns

    if root_driven:
        return _ns._wait_for_root_services_ready(roster, timeout_s=timeout_s)
    return _ns._wait_for_services_ready(roster, timeout_s=timeout_s)


# ─── the readiness leg ──────────────────────────────────────────────────────


def _health_verdicts(status: dict[str, Any] | None) -> dict[str, str]:
    """Latest health verdicts by unit id from one status snapshot."""
    if status is None:
        return {}
    raw_raw = status.get("health")
    raw = cast("dict[str, object]", raw_raw) if isinstance(raw_raw, dict) else {}
    verdicts: dict[str, str] = {}
    for unit_id, row_raw in raw.items():
        if not isinstance(row_raw, dict):
            continue
        verdict = cast("dict[str, object]", row_raw).get("last_verdict")
        if isinstance(verdict, str):
            verdicts[unit_id] = verdict
    return verdicts


def _unit_ready(unit: dict[str, Any] | None, verdict: str | None) -> bool:
    """Whether one unit reads ready: running, and not judged down.

    A verdict the runner has not produced yet (its first round lands on the
    health interval) passes — the same "cannot judge, cannot gate" rule a
    probe-less service has on the session path. `down` / `port-taken` are
    positive evidence of not-serving and do gate.
    """
    if unit is None:
        return False
    if unit.get("state") != "running":
        return False
    return verdict not in {"down", "port-taken"}


def _unit_gone(unit: dict[str, Any] | None) -> bool:
    """Whether a non-running unit is dead-dead (no retry on its way).

    `stopped` is the no-process state; a spawn-failing unit rests in `backoff`
    with `last_error` set while the supervisor retries — that is a unit that
    will not come up by waiting either. A backoff without `last_error` is a
    crashed generation the supervisor is actively replacing, so it is not gone.
    """
    if unit is None:
        return True
    state = unit.get("state")
    if state == "stopped":
        return True
    return state == "backoff" and bool(unit.get("last_error"))


def _wait_for_root_services_ready(
    specs: tuple[ServiceSpec, ...], timeout_s: float
) -> ReadinessWait:
    """Poll the root's status until every unit is ready, tiered like `_probe`.

    Same contract as the session wait: critical services keep the whole bound
    and are the only ones that can end it unready; non-critical services get
    the short window and are reported without blocking. The status surface is
    the root's own (`units[].state` + its health verdicts); a status that stops
    answering counts every remaining unit as gone.
    """
    from cli.commands._probe import _SESSION_GONE_CONFIRMATIONS, CRITICAL_SERVICE_SESSIONS
    from shared.deploy_timing import NON_CRITICAL_SERVICE_READY_TIMEOUT_S

    client = _root_client()
    started_at = time.monotonic()
    deadline = started_at + timeout_s
    non_critical_deadline = started_at + NON_CRITICAL_SERVICE_READY_TIMEOUT_S
    critical = tuple(s for s in specs if s.session in CRITICAL_SERVICE_SESSIONS)
    non_critical = {s.session: s for s in specs if s.session not in CRITICAL_SERVICE_SESSIONS}
    non_critical_unready: list[ServiceSpec] = []
    gone_streak: dict[str, int] = {}
    non_critical_gone_streak: dict[str, int] = {}
    while True:
        status = _root_status(client)
        units = _root_units(status) if status is not None else {}
        verdicts = _health_verdicts(status)
        for name, spec in list(non_critical.items()):
            if _unit_ready(units.get(name), verdicts.get(name)):
                del non_critical[name]
                continue
            gone = status is None or _unit_gone(units.get(name))
            non_critical_gone_streak[name] = (
                0 if not gone else non_critical_gone_streak.get(name, 0) + 1
            )
            if non_critical_gone_streak[name] >= _SESSION_GONE_CONFIRMATIONS:
                del non_critical[name]
                non_critical_unready.append(spec)
        if non_critical and time.monotonic() >= non_critical_deadline:
            non_critical_unready.extend(non_critical.values())
            non_critical.clear()
        unready = tuple(
            s for s in critical if not _unit_ready(units.get(s.session), verdicts.get(s.session))
        )
        if not unready and not non_critical:
            return ReadinessWait(
                (),
                time.monotonic() - started_at,
                sessions_gone=False,
                non_critical_unready=tuple(non_critical_unready),
            )
        if unready:
            for spec in unready:
                gone = status is None or _unit_gone(units.get(spec.session))
                gone_streak[spec.session] = 0 if not gone else gone_streak.get(spec.session, 0) + 1
            gone_all = all(gone_streak[s.session] >= _SESSION_GONE_CONFIRMATIONS for s in unready)
            if gone_all or time.monotonic() >= deadline:
                return ReadinessWait(
                    unready,
                    time.monotonic() - started_at,
                    sessions_gone=gone_all,
                    non_critical_unready=tuple(non_critical_unready),
                )
        _poll_sleep(_READY_POLL_INTERVAL_S)


# ─── the stop leg ───────────────────────────────────────────────────────────


def _root_tree_plan(preserve: frozenset[str] = frozenset()) -> list[str]:
    """Session-named units a stop with `preserve` would stop; empty when no root answers."""
    status = _root_status(_root_client())
    if status is None:
        return []
    return sorted(
        session_name(unit_id) for unit_id in _root_units(status) if unit_id not in preserve
    )


def _stop_root_service_tree(
    *, preserve: frozenset[str], timeout_s: float = _ROOT_STOP_TIMEOUT_S
) -> None:
    """Stop the root-owned tree — everything, or only the units not preserved.

    `preserve` holds bare service names (pause's browser, `--keep-service`).
    Stopping every unit also stops the root itself; keeping at least one unit
    leaves the root running to host it. Raises `_RootDriverError` when the
    tree cannot be brought down; the caller's phase accounting reports it.
    """
    from shared.paths import root_run_dir

    client = _root_client()
    status = _root_status(client)
    if status is None:
        print("  ava-root: not running — no tree to stop")
        return
    units = _root_units(status)
    stop_ids = sorted(unit_id for unit_id in units if unit_id not in preserve)
    if not stop_ids:
        print("  ava-root: every unit is preserved — tree left running")
        return
    if preserve:
        kept = sorted(session_name(unit_id) for unit_id in units if unit_id in preserve)
        print(
            "  ava-root: stopping "
            + ", ".join(session_name(unit_id) for unit_id in stop_ids)
            + (f" (kept: {', '.join(kept)})" if kept else "")
        )
        for unit_id in stop_ids:
            _call_ok(client.down(unit_id), f"down {unit_id}")
            print(f"  ✓ ava-root unit {session_name(unit_id)} stopped")
        return
    print(f"  ava-root: stopping the whole tree ({len(stop_ids)} unit(s)) and the root")
    _stop_root_process(root_run_dir(), client, status, timeout_s=timeout_s)
    print("  ✓ ava-root stopped (tree down, root exited)")
