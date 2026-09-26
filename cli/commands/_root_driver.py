"""One application root for start, stop and recovery, with native platform custody."""

from __future__ import annotations

import json
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, NamedTuple, cast

from cli.commands._probe import ReadinessWait
from cli.commands._repo import ServiceSpec, session_name
from cli.start_runtime import StartRuntime
from ops.service_spec import db_access, profile_marker
from shared.cluster.derive import runner_db_url_projection
from shared.config import settings
from shared.machine import MachineRoles

_ROOT_SOCKET_NAME = "ava-root.sock"
_ROOT_STDOUT_LOG = "root.stdout.log"
_ROOT_STDERR_LOG = "root.stderr.log"
_WIRING_REF = "services.ava_root_glue.glue:build_wiring"
_HELPER_PROTOCOLS = ("root_stop_intent_v1", "helper_shutdown_v1")

# Root binds IPC after spawning its units.
_ROOT_READY_TIMEOUT_S = 30.0
# Unit stop windows accumulate in dependency order.
_ROOT_STOP_TIMEOUT_S = 90.0
_READY_POLL_INTERVAL_S = 0.5
_poll_sleep = time.sleep  # a named seam tests can patch (the _probe pattern)
# Keep the child unreaped until CLI exit; a published PID cannot be recycled.
_direct_root_child: subprocess.Popen[bytes] | None = None


class _RootDriverError(RuntimeError):
    """The root-driven tree could not be brought up, reconciled or stopped."""


def complete_boot_start() -> None:
    """Hand Linux systemd the verified root after ordinary readiness succeeds."""
    if sys.platform != "linux":
        return
    from shared.os_boot_unit import in_boot_unit, publish_root_ready
    from shared.paths import ava_home
    from shared.root_control.client import native_identity

    home = ava_home()
    if not in_boot_unit(home):
        return
    snapshot = _root_status(_root_client())
    if snapshot is None:
        raise _RootDriverError("cannot hand systemd an unobservable root")
    _require_root_owner(snapshot)
    publish_root_ready(home, native_identity(snapshot["root"]))


class LaunchOutcome(NamedTuple):
    """The complete requested roster and units whose launch failed."""

    started: tuple[ServiceSpec, ...]
    failed: tuple[str, ...]


def _service_extra_env(spec: ServiceSpec) -> dict[str, str]:
    """Bind profile and database login to one service, never its parent."""
    from cli.commands._data_plane import db_delivery
    from shared.lgtm_local import BACKENDS, service_environment

    extra = service_environment(spec.session) if spec.session in BACKENDS else {}
    marker = profile_marker(spec)
    if marker is not None:
        extra["AVA_PROCESS_PROFILE"] = marker
    cls = db_access(spec)
    delivery = db_delivery(cls) if cls is not None else {}
    if delivery:
        extra.update(delivery)
    elif marker == "agent":
        extra["AVA_DB_URL"] = runner_db_url_projection(settings.data_plane.db_url)
    return extra


def _root_tree_roster(roles: MachineRoles, launch_skip: set[str]) -> tuple[ServiceSpec, ...]:
    """The capability/config-selected units for this root."""
    from cli.commands._repo import _services_for_roles_annotated

    return tuple(
        spec
        for spec, reason in _services_for_roles_annotated(roles)
        if reason is None and spec.session not in launch_skip
    )


def _root_client(*, timeout: float = 5.0) -> Any:
    """A blocking client bound to this cluster's root control socket."""
    from shared.paths import root_run_dir
    from shared.root_control.client import RootClient

    return RootClient(root_run_dir() / _ROOT_SOCKET_NAME, timeout=timeout)


def _root_status(client: Any) -> dict[str, Any] | None:
    """The daemon's status result, or None when no root answers (yet)."""
    from shared.root_control.client import RootClientError

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
    """macOS service ancestry is mandatory, independent of a runtime switch."""
    return sys.platform == "darwin"


def _helper_wire_ok() -> bool:
    """Whether the permission helper answers on this cluster's socket."""
    from services.permissions_helper import client as helper_client

    try:
        ping = helper_client.ping()
        return all(ping.get(capability) is True for capability in _HELPER_PROTOCOLS)
    except Exception:
        return False


def _require_root_owner(status: dict[str, Any]) -> None:
    """Bind every reused or stopped root to its required live native parent."""
    import psutil

    from services.permissions_helper import client as helper_client
    from shared.native_process.ownership import OwnedProcess
    from shared.paths import root_run_dir
    from shared.root_control.client import native_identity

    root = native_identity(status.get("root"))
    if not root.live():
        raise _RootDriverError("recorded root native birth is no longer live")
    if not _helper_spawn_committed():
        return
    ping = helper_client.ping()
    if not all(ping.get(capability) is True for capability in _HELPER_PROTOCOLS):
        raise _RootDriverError(f"required helper lacks lifecycle protocols: {_HELPER_PROTOCOLS}")
    helper_pid = ping.get("pid")
    if isinstance(helper_pid, bool) or not isinstance(helper_pid, int) or helper_pid <= 1:
        raise _RootDriverError("required helper omitted its native process")
    helper = OwnedProcess.capture(psutil.Process(helper_pid))
    keeper = helper_client.root_status()
    if (
        keeper.get("pid") != root.pid
        or keeper.get("run_dir") != str(root_run_dir())
        or psutil.Process(root.pid).ppid() != helper.pid
        or not helper.live()
        or not root.live()
    ):
        raise _RootDriverError("root is outside this home's signed-helper custody")


def _root_argv(run_dir: Path, manifests: Path, runtime: StartRuntime | None = None) -> list[str]:
    """The daemon command line: the K3 launch face the root package freezes."""
    arguments = [
        "--run-dir",
        str(run_dir),
        "--manifests",
        str(manifests),
        "--wiring",
        _WIRING_REF,
    ]
    if runtime is not None:
        return runtime.module_argv("services.ava_root", *arguments)
    return [sys.executable, "-m", "services.ava_root", *arguments]


def _root_child_env() -> dict[str, str]:
    """The root env, including the proof it may pass only to agent-host."""
    from shared.env_registry import manifest_certification_secret_env
    from shared.session_env import managed_service_env

    return managed_service_env(settings.general.service_path) | manifest_certification_secret_env()


def _tree_manifest(
    roster: tuple[ServiceSpec, ...],
    repo: Path,
    *,
    roles: MachineRoles,
    runtime: StartRuntime | None = None,
) -> dict[str, object]:
    """Prepare launch inputs without changing any running generation's seed."""
    from services.ava_root_glue.manifests import build_manifest

    return build_manifest(
        capabilities=sorted(roles),
        repo_root=repo,
        specs=roster,
        environments={spec.session: _service_extra_env(spec) for spec in roster},
        release=None if runtime is None else runtime.release,
    )


def _log_tail(path: Path, lines: int = 20) -> str:
    """The last `lines` of a log file, for an actionable bring-up failure."""
    try:
        content = path.read_text(errors="replace").splitlines()
    except OSError:
        return "(no log)"
    return "\n".join(content[-lines:]) or "(empty log)"


def _spawn_direct(
    run_dir: Path,
    repo: Path,
    manifests: Path,
    env: dict[str, str],
    runtime: StartRuntime | None = None,
) -> subprocess.Popen[bytes]:
    """Launch the root daemon detached (own session), logging under the run dir."""
    global _direct_root_child  # noqa: PLW0603 — retain the unreaped native child through CLI exit
    stdout = (run_dir / _ROOT_STDOUT_LOG).open("ab")
    stderr = (run_dir / _ROOT_STDERR_LOG).open("ab")
    try:
        _direct_root_child = subprocess.Popen(
            _root_argv(run_dir, manifests, runtime),
            cwd=repo if runtime is None else runtime.cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            start_new_session=True,
            close_fds=True,
        )
    finally:
        stdout.close()
        stderr.close()
    return _direct_root_child


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


def _seed_via_helper(
    run_dir: Path,
    repo: Path,
    manifests: Path,
    env: dict[str, str],
    runtime: StartRuntime | None = None,
) -> None:
    """Seed the root keeper and wait for it to report the root `running`."""
    from services.permissions_helper import client as helper_client

    try:
        from shared.atomic_io import write_text_atomic

        seed: helper_client.RootSeedConfig = {
            "argv": _root_argv(run_dir, manifests, runtime),
            "cwd": str(repo if runtime is None else runtime.cwd),
            "run_dir": str(run_dir),
            "stdout": str(run_dir / _ROOT_STDOUT_LOG),
            "stderr": str(run_dir / _ROOT_STDERR_LOG),
            "env": env,
        }
        write_text_atomic(run_dir / "seed.json", json.dumps(seed), mode=0o600, sync_parent=True)
        wire = helper_client.seed_root(seed)
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


def _bring_up_root(
    run_dir: Path,
    repo: Path,
    manifests: Path,
    client: Any,
    env: dict[str, str],
    runtime: StartRuntime | None = None,
) -> dict[str, Any]:
    """Start through the required platform owner; never change ownership on failure."""
    if sys.platform == "win32":
        raise _RootDriverError(
            "native Windows root supervision requires its transport and Job ownership "
            "adapter; session service startup is no longer supported"
        )
    if _helper_spawn_committed():
        if not _helper_wire_ok():
            raise _RootDriverError(
                f"the macOS helper is unreachable or lacks {_HELPER_PROTOCOLS}; prepare and "
                "activate a reviewed signed helper supporting durable root stop before "
                "starting services. Refusing direct spawning outside the permission ancestry"
            )
        _seed_via_helper(run_dir, repo, manifests, env, runtime)
        return _await_root_status(client, run_dir)
    proc = _spawn_direct(run_dir, repo, manifests, env, runtime)
    print(f"  + ava-root spawned directly (pid {proc.pid})")
    return _await_root_status(client, run_dir, proc=proc)


def _changed_units(manifest: dict[str, object], status: dict[str, Any]) -> set[str]:
    """A running root may be reused only for the exact requested launch inputs."""
    from services.ava_root.manifest import UnitManifest

    units = _root_units(status)
    desired = [
        UnitManifest.from_mapping(row, origin="requested tree")
        for row in cast("list[dict[str, object]]", manifest["units"])
    ]
    return {
        item.id
        for item in desired
        if units.get(item.id, {}).get("manifest_digest") != item.digest()
    }


def _stop_root_process(
    run_dir: Path, client: Any, status: dict[str, Any], *, timeout_s: float
) -> None:
    """Stop through the keeper on macOS, or SIGTERM; await native root exit."""
    from shared.root_control.client import native_identity

    _require_root_owner(status)
    identity = native_identity(status["root"])
    if _helper_spawn_committed():
        if not _helper_wire_ok():
            raise _RootDriverError(
                "the required macOS helper is unreachable — refusing to "
                "signal a helper-seeded root directly (the keeper would restart it); stop it "
                "through the helper"
            )
        from services.permissions_helper import client as helper_client

        try:
            reply = helper_client.stop_root()
        except Exception as exc:
            raise _RootDriverError(f"root_stop over the helper failed: {exc}") from exc
        if reply.get("stop_requested") is not True:
            raise _RootDriverError("helper did not retain root stop intent")
    else:
        identity.send_signal(signal.SIGTERM)
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        alive = identity.live()
        if not alive and _root_status(client) is None:
            return
        _poll_sleep(_READY_POLL_INTERVAL_S)
    raise _RootDriverError(f"ava-root at {run_dir} did not stop within {timeout_s:.0f}s")


def _reconcile_units(
    roster: tuple[ServiceSpec, ...], client: Any, status: dict[str, Any]
) -> dict[str, Any]:
    """Resume stopped units within the same immutable root generation."""
    units = _root_units(status)
    for spec in roster:
        unit = units[spec.session]
        if unit.get("state") != "running" or unit.get("desired") != "running":
            reason = unit.get("last_error") or unit.get("last_exit") or unit.get("state")
            print(f"  ↑ ava-root unit {session_name(spec.session)} ({reason}) — bringing it up")
            _call_ok(client.up(spec.session), f"up {spec.session}")
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


def _require_same_generation(
    manifest: dict[str, object],
    status: dict[str, Any],
    roster: tuple[ServiceSpec, ...],
    *,
    reconcile: bool,
) -> None:
    changed = _changed_units(manifest, status)
    if status["root"].get("launch_digest") != manifest["launch_digest"]:
        changed.add("root launch inputs")
    if reconcile:
        changed |= _root_units(status).keys() - {spec.session for spec in roster}
    if changed:
        raise _RootDriverError(
            "requested services change the immutable root generation: "
            + ", ".join(sorted(changed))
            + "; run ava stop before starting with changed services or configuration"
        )


def admit_live_start(
    roster: tuple[ServiceSpec, ...],
    repo: Path,
    roles: MachineRoles,
    *,
    reconcile: bool,
    runtime: StartRuntime | None = None,
) -> bool:
    """Observe before any converge/schema write; reuse only identical inputs."""
    from cli.commands._start_generation import launch_digest

    status = _root_status(_root_client())
    if status is None:
        _require_root_absent()
        return False
    _require_root_owner(status)
    manifest = _tree_manifest(roster, repo, roles=roles, runtime=runtime)
    from shared.paths import ava_home

    manifest["launch_digest"] = launch_digest(
        repo, _root_child_env(), home=ava_home(), runtime=runtime
    )
    _require_same_generation(manifest, status, roster, reconcile=reconcile)
    return True


def _ensure_root_service_tree(
    roster: tuple[ServiceSpec, ...],
    repo: Path,
    *,
    roles: MachineRoles,
    reconcile: bool,
    runtime: StartRuntime | None = None,
) -> LaunchOutcome:
    """Reuse an identical generation; changing its inputs requires prior stop."""
    from services.ava_root_glue.manifests import write_manifest
    from shared.paths import ava_home, root_manifests_path, root_run_dir

    run_dir = root_run_dir()
    if runtime is not None:
        runtime.validate(ava_home())
    env = _root_child_env()
    manifest = _tree_manifest(roster, repo, roles=roles, runtime=runtime)
    from cli.commands._start_generation import launch_digest

    manifest["launch_digest"] = launch_digest(repo, env, home=ava_home(), runtime=runtime)
    manifests = root_manifests_path()
    client = _root_client()
    try:
        status = _root_status(client)
        if status is not None:
            _require_root_owner(status)
            _require_same_generation(manifest, status, roster, reconcile=reconcile)
        units = _root_units(status) if status is not None else {}
        for spec in roster:
            unit = units.get(spec.session)
            if unit is not None and unit.get("state") == "running":
                continue
            if spec.session == "frontend" and (runtime is None or runtime.release is None):
                from cli.commands._repo import _ensure_frontend_deps

                _ensure_frontend_deps(repo)
        if status is None:
            _require_root_absent()
            write_manifest(manifests, manifest)
            status = _bring_up_root(run_dir, repo, manifests, client, env, runtime)
            _require_root_owner(status)
            _require_same_generation(manifest, status, roster, reconcile=reconcile)
        status = _reconcile_units(roster, client, status)
    except _RootDriverError as exc:
        print(f"  ✗ ava-root tree bring-up failed: {exc}", file=sys.stderr)
        return LaunchOutcome(roster, tuple(session_name(spec.session) for spec in roster))
    return _classify_units(roster, status)


def _start_roster(roles: MachineRoles, launch_skip: set[str]) -> tuple[ServiceSpec, ...]:
    """Return the sole root-owned service roster."""
    return _root_tree_roster(roles, launch_skip)


def _launch_service_tree(
    roster: tuple[ServiceSpec, ...],
    repo: Path,
    roles: MachineRoles,
    *,
    reconcile: bool,
    runtime: StartRuntime | None = None,
) -> LaunchOutcome:
    """Start the requested services through their root owner."""
    return _ensure_root_service_tree(
        roster, repo, roles=roles, reconcile=reconcile, runtime=runtime
    )


def _wait_for_service_tree(
    roster: tuple[ServiceSpec, ...],
    *,
    timeout_s: float,
) -> ReadinessWait:
    """Verify the same root-owned roster after launch."""
    return _wait_for_root_services_ready(roster, timeout_s=timeout_s)


def _health_verdicts(
    specs: tuple[ServiceSpec, ...], status: dict[str, Any] | None
) -> dict[str, str]:
    """Probe now; a cached root health round cannot certify a new generation."""
    if status is None:
        return {}
    verdicts: dict[str, str] = {}
    for spec in specs:
        if spec.identity_probe is None:
            continue
        try:
            verdicts[spec.session] = spec.identity_probe().verdict.value
        except Exception:
            verdicts[spec.session] = "unavailable"
    return verdicts


def _unit_ready(unit: dict[str, Any] | None, verdict: str | None) -> bool:
    """Require a live generation and fresh positive protocol evidence."""
    if unit is None:
        return False
    if unit.get("state") != "running":
        return False
    return verdict == "alive"


def _unit_gone(unit: dict[str, Any] | None) -> bool:
    """A stopped unit cannot become ready without explicit reconciliation."""
    return unit is not None and unit.get("state") == "stopped"


def _fresh_readiness_round(
    client: Any, specs: tuple[ServiceSpec, ...]
) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    """Bind this round's protocol evidence to unchanged native generations."""
    status = _root_status(client)
    units = _root_units(status) if status is not None else {}
    verdicts = _health_verdicts(specs, status)
    after = _root_status(client)
    after_units = _root_units(after) if after is not None else {}
    for name in tuple(verdicts):
        unit = units.get(name)
        current = after_units.get(name)
        if (
            unit is None
            or current is None
            or unit.get("state") != "running"
            or current.get("state") != "running"
            or any(unit.get(key) != current.get(key) for key in ("pid", "create_time", "starttime"))
        ):
            verdicts.pop(name, None)
    return after_units, verdicts


def _unready_services(
    specs: tuple[ServiceSpec, ...],
    units: dict[str, dict[str, Any]],
    verdicts: dict[str, str],
) -> tuple[ServiceSpec, ...]:
    return tuple(
        spec
        for spec in specs
        if not _unit_ready(units.get(spec.session), verdicts.get(spec.session))
    )


def _confirmed_gone(specs: tuple[ServiceSpec, ...], streak: dict[str, int]) -> bool:
    from cli.commands._probe import _SESSION_GONE_CONFIRMATIONS

    return bool(specs) and all(
        streak[spec.session] >= _SESSION_GONE_CONFIRMATIONS for spec in specs
    )


def _next_gone_streak(
    specs: tuple[ServiceSpec, ...], units: dict[str, dict[str, Any]], previous: dict[str, int]
) -> dict[str, int]:
    return {
        spec.session: previous.get(spec.session, 0) + 1
        if _unit_gone(units.get(spec.session))
        else 0
        for spec in specs
    }


def _wait_for_root_services_ready(
    specs: tuple[ServiceSpec, ...], timeout_s: float
) -> ReadinessWait:
    """Every success uses one fresh whole-roster observation, never sticky ALIVE."""
    from cli.commands._probe import CRITICAL_SERVICE_SESSIONS
    from shared.deploy_timing import NON_CRITICAL_SERVICE_READY_TIMEOUT_S

    client = _root_client()
    started_at = time.monotonic()
    deadline = started_at + timeout_s
    non_critical_deadline = started_at + NON_CRITICAL_SERVICE_READY_TIMEOUT_S
    critical = tuple(s for s in specs if s.session in CRITICAL_SERVICE_SESSIONS)
    non_critical = tuple(s for s in specs if s.session not in CRITICAL_SERVICE_SESSIONS)
    gone_streak: dict[str, int] = {}
    while True:
        units, verdicts = _fresh_readiness_round(client, specs)
        gone_streak = _next_gone_streak(specs, units, gone_streak)
        unready = _unready_services(critical, units, verdicts)
        non_critical_unready = _unready_services(non_critical, units, verdicts)
        now = time.monotonic()
        gone_all = _confirmed_gone(unready, gone_streak)
        non_critical_settled = (
            not non_critical_unready
            or now >= non_critical_deadline
            or _confirmed_gone(non_critical_unready, gone_streak)
        )
        if (not unready and non_critical_settled) or gone_all or now >= deadline:
            return ReadinessWait(
                unready,
                now - started_at,
                sessions_gone=gone_all,
                non_critical_unready=non_critical_unready,
            )
        _poll_sleep(_READY_POLL_INTERVAL_S)


def _require_root_absent() -> None:
    from services.ava_root.custody import require_clear
    from services.ava_root.singleton import acquire_instance_lock, release_instance_lock
    from shared.paths import root_run_dir

    run_dir = root_run_dir()
    require_clear(run_dir)
    fd = acquire_instance_lock(run_dir)
    release_instance_lock(fd)


def _root_tree_plan(preserve: frozenset[str] = frozenset()) -> list[str]:
    """Display names for the selected exact root units."""
    return sorted(name for name, unit in _root_tree_selection().items() if unit not in preserve)


def _root_tree_selection() -> dict[str, str]:
    """Map the home's qualified display names to the root's exact unit IDs."""
    status = _root_status(_root_client())
    if status is None:
        _require_root_absent()
        return {}
    return {session_name(unit_id): unit_id for unit_id in _root_units(status)}


def _stop_dormant_helper_root(deadline: float) -> None:
    """Revoke a pending keeper restart even when no root IPC is serving."""
    if not _helper_spawn_committed():
        return
    from services.permissions_helper import client as helper_client
    from services.permissions_helper.launchd_job import (
        _retirement_query,
        helper_job_domain,
        helper_job_label,
    )

    try:
        keeper = helper_client.root_status()
    except helper_client.PermissionsHelperError as exc:
        target = f"{helper_job_domain()}/{helper_job_label()}"
        if _retirement_query(target, deadline) is None:
            return
        raise _RootDriverError(
            "loaded helper custody is unavailable; stop remains incomplete"
        ) from exc
    if keeper["state"] == "unseeded" and not keeper["seeded"]:
        return
    if keeper.get("pid") is not None or keeper["state"] == "conflict":
        raise _RootDriverError("helper owns a root without usable IPC; custody requires recovery")
    if not _helper_wire_ok() or helper_client.stop_root().get("stop_requested") is not True:
        raise _RootDriverError("helper did not persist stop of its dormant root")
    _require_root_absent()


def _stop_root_service_tree(
    *,
    preserve: frozenset[str],
    timeout_s: float = _ROOT_STOP_TIMEOUT_S,
    force: bool = False,
    selected: frozenset[str] | None = None,
) -> None:
    """Stop the root-owned tree — everything, or only the units not preserved.

    `preserve` holds bare service names (pause's browser, `--keep-service`).
    Stopping every unit also stops the root itself; keeping at least one unit
    leaves the root running to host it. Raises `_RootDriverError` when the
    tree cannot be brought down; the caller's phase accounting reports it.
    """
    from shared.paths import root_run_dir

    deadline = time.monotonic() + timeout_s
    client = _root_client(timeout=timeout_s)
    status = _root_status(client)
    if status is None:
        _require_root_absent()
        if selected is not None:
            return
        _stop_dormant_helper_root(deadline)
        print("  ava-root: no live owner or retained service custody")
        return
    units = _root_units(status)
    _require_root_owner(status)
    preserved = set(units) & preserve
    if selected is not None:
        preserved |= set(units) - selected
        if not (set(units) & selected):
            return
    stop_ids = sorted(set(units) - preserved)
    if not stop_ids and preserved:
        print("  ava-root: every unit is preserved — tree left running")
        return
    for unit_id in stop_ids:
        remaining_s = deadline - time.monotonic()
        if remaining_s <= 0:
            raise _RootDriverError("service stop deadline expired; custody retained")
        client = _root_client(timeout=remaining_s)
        response = client.force_down(unit_id) if force else client.down(unit_id)
        _call_ok(response, f"down {unit_id}")
        print(f"  ✓ ava-root unit {session_name(unit_id)} stopped")
    if preserved:
        return
    print(f"  ava-root: stopping the whole tree ({len(stop_ids)} unit(s)) and the root")
    _stop_root_process(
        root_run_dir(), client, status, timeout_s=max(0.0, deadline - time.monotonic())
    )
    print("  ✓ ava-root stopped (tree down, root exited)")
