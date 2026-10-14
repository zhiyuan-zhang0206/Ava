"""One application root for start, stop and recovery."""

from __future__ import annotations

import json
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, NamedTuple, cast

from base.cluster import session_name
from base.cluster.derive import runner_db_url_projection
from base.cluster.machine import MachineIdentity, MachineRoles
from base.config import settings
from base.host.permissions_helper.client import HelperFactory
from base.log import logger
from base.sessions.helperproc import HELPER_PROTOCOLS, helper_spawn_committed, helper_wire_ok
from cli.commands.data_plane.config import data_plane_config_from_settings
from cli.commands.probe import ReadinessWait
from cli.start_runtime import StartRuntime
from services.supervision.roster import service_spec
from services.supervision.roster.service_spec import ServiceSpec

_ROOT_SOCKET_NAME = "ava-root.sock"
_ROOT_STDOUT_LOG = "root.stdout.log"
_ROOT_STDERR_LOG = "root.stderr.log"
_WIRING_REF = "services.supervision.ava_root_glue.glue:build_wiring"

# Root binds IPC after spawning its units.
_ROOT_READY_TIMEOUT_S = 30.0
# Unit stop windows accumulate in dependency order.
_ROOT_STOP_TIMEOUT_S = 90.0
_READY_POLL_INTERVAL_S = 0.5
poll_sleep = time.sleep  # a named seam tests can patch (the _probe pattern)


class RootDriverError(RuntimeError):
    """The root-driven tree could not be brought up, reconciled or stopped."""


def complete_boot_start(*, helper: HelperFactory) -> None:
    """Hand Linux systemd the verified root after ordinary readiness succeeds."""
    if sys.platform != "linux":
        return
    from base.host.system.boot_unit import in_boot_unit, publish_root_ready
    from base.native_process.root_control.client import native_identity
    from base.paths import ava_home

    home = ava_home()
    if not in_boot_unit():
        return
    snapshot = root_status(root_client())
    if snapshot is None:
        raise RootDriverError("cannot hand systemd an unobservable root")
    require_root_owner(snapshot, helper=helper)
    publish_root_ready(home, native_identity(snapshot["root"]))


class LaunchOutcome(NamedTuple):
    """The complete requested roster and units whose launch failed."""

    started: tuple[ServiceSpec, ...]
    failed: tuple[str, ...]


def service_extra_env(spec: ServiceSpec, *, remote_data_plane: bool) -> dict[str, str]:
    """Bind profile, database login and API token to one service, never its parent."""
    from base.telemetry.lgtm_local import BACKENDS, service_environment
    from cli.commands.data_plane.bringup import api_delivery, db_delivery

    extra = service_environment(spec.session) if spec.session in BACKENDS else {}
    marker = service_spec.profile_marker(spec)
    if marker is not None:
        extra["AVA_PROCESS_PROFILE"] = marker
    cls = service_spec.db_access(spec)
    delivery = db_delivery(cls, remote_data_plane=remote_data_plane) if cls is not None else {}
    if delivery:
        extra.update(delivery)
    elif marker == "agent":
        extra["AVA_DB_URL"] = runner_db_url_projection()
    if (api := service_spec.api_access(spec)) is not None:
        extra.update(api_delivery(api, remote_data_plane=remote_data_plane))
    if spec.plugin_config is not None:
        from base.host.env.registry import SERVICE_PLUGIN_CONFIG_ENV
        from base.packages.plugins.config_registration import service_config_packet

        extra[SERVICE_PLUGIN_CONFIG_ENV] = service_config_packet(*spec.plugin_config)
    return extra


def root_tree_roster(
    roles: MachineRoles, launch_skip: set[str], *, machine: MachineIdentity
) -> tuple[ServiceSpec, ...]:
    """The capability/config-selected units for this root."""
    from services.supervision.roster.spec import services_for_capabilities_annotated

    return tuple(
        spec
        for spec, reason in services_for_capabilities_annotated(roles, machine=machine)
        if reason is None and spec.session not in launch_skip
    )


def root_client(*, timeout: float = 5.0) -> Any:
    """A blocking client bound to this cluster's root control socket."""
    from base.native_process.root_control.client import RootClient
    from base.paths import root_run_dir

    return RootClient(root_run_dir() / _ROOT_SOCKET_NAME, timeout=timeout)


def root_status(client: Any) -> dict[str, Any] | None:
    """The daemon's status result, or None when no root answers (yet)."""
    from base.native_process.root_control.client import RootClientError

    try:
        response = client.status()
    except RootClientError:
        return None
    result = response.get("result")
    if response.get("ok") and isinstance(result, dict):
        return cast("dict[str, Any]", result)
    return None


def root_units(status: dict[str, Any]) -> dict[str, dict[str, Any]]:
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
    """Raise `RootDriverError` when a K1 reply is not ok."""
    if not response.get("ok"):
        raise RootDriverError(f"{what} failed: {response.get('code')}: {response.get('error')}")


def require_root_owner(status: dict[str, Any], *, helper: HelperFactory) -> None:
    """Bind every reused or stopped root to its required live native parent."""
    import psutil

    from base.native_process.ownership import OwnedProcess
    from base.native_process.root_control.client import native_identity
    from base.paths import root_run_dir

    root = native_identity(status.get("root"))
    if not root.live():
        raise RootDriverError("recorded root native birth is no longer live")
    if not helper_spawn_committed():
        return
    ping = helper().ping()
    if not all(ping.get(capability) is True for capability in HELPER_PROTOCOLS):
        raise RootDriverError(f"required helper lacks lifecycle protocols: {HELPER_PROTOCOLS}")
    helper_pid = ping.get("pid")
    if isinstance(helper_pid, bool) or not isinstance(helper_pid, int) or helper_pid <= 1:
        raise RootDriverError("required helper omitted its native process")
    helper_process = OwnedProcess.capture(psutil.Process(helper_pid))
    keeper = helper().root_status()
    if (
        keeper.get("pid") != root.pid
        or keeper.get("run_dir") != str(root_run_dir())
        or psutil.Process(root.pid).ppid() != helper_process.pid
        or not helper_process.live()
        or not root.live()
    ):
        raise RootDriverError("root is outside this home's signed-helper custody")


def root_argv(run_dir: Path, manifests: Path, runtime: StartRuntime | None = None) -> list[str]:
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
        return runtime.module_argv("services.supervision.ava_root", *arguments)
    return [sys.executable, "-m", "services.supervision.ava_root", *arguments]


def root_child_env() -> dict[str, str]:
    """The root env."""
    from base.sessions.env_forwarding import managed_service_env

    return managed_service_env(settings.general.service_path)


def tree_manifest(
    roster: tuple[ServiceSpec, ...],
    repo: Path,
    *,
    roles: MachineRoles,
) -> dict[str, object]:
    """Prepare launch inputs without changing any running generation's seed."""
    from services.supervision.ava_root_glue.manifests import build_manifest

    remote = data_plane_config_from_settings().is_remote
    return build_manifest(
        capabilities=sorted(roles),
        repo_root=repo,
        specs=roster,
        environments={s.session: service_extra_env(s, remote_data_plane=remote) for s in roster},
    )


def _log_tail(path: Path, lines: int = 20) -> str:
    """The last `lines` of a log file, for an actionable bring-up failure."""
    try:
        content = path.read_text(errors="replace").splitlines()
    except OSError:
        return "(no log)"
    return "\n".join(content[-lines:]) or "(empty log)"


def spawn_direct(
    run_dir: Path,
    repo: Path,
    manifests: Path,
    env: dict[str, str],
    runtime: StartRuntime | None = None,
    *,
    retained_children: list[subprocess.Popen[bytes]],
) -> subprocess.Popen[bytes]:
    """Launch the root daemon detached (own session), logging under the run dir."""
    stdout = (run_dir / _ROOT_STDOUT_LOG).open("ab")
    stderr = (run_dir / _ROOT_STDERR_LOG).open("ab")
    try:
        child = subprocess.Popen(
            root_argv(run_dir, manifests, runtime),
            cwd=repo if runtime is None else runtime.cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            start_new_session=True,
            close_fds=True,
        )
        # The CLI caller retains its direct children through its final telemetry drain.
        # Dropping this handle would let a later Popen reap a published root PID.
        retained_children.append(child)
    finally:
        stdout.close()
        stderr.close()
    return child


def await_root_status(
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
            raise RootDriverError(
                f"ava-root exited before serving (rc={proc.returncode}); log tail:\n{tail}"
            )
        status = root_status(client)
        if status is not None:
            return status
        poll_sleep(_READY_POLL_INTERVAL_S)
    tail = _log_tail(run_dir / _ROOT_STDERR_LOG)
    raise RootDriverError(
        f"ava-root did not become ready within {timeout_s:.0f}s; log tail:\n{tail}"
    )


def seed_via_helper(
    run_dir: Path,
    repo: Path,
    manifests: Path,
    env: dict[str, str],
    runtime: StartRuntime | None = None,
    *,
    helper: HelperFactory,
) -> None:
    """Seed the root keeper and wait for it to report the root `running`."""
    from base.host.permissions_helper.client import RootSeedConfig

    try:
        from base.host.atomic_io import write_text_atomic

        seed: RootSeedConfig = {
            "argv": root_argv(run_dir, manifests, runtime),
            "cwd": str(repo if runtime is None else runtime.cwd),
            "run_dir": str(run_dir),
            "stdout": str(run_dir / _ROOT_STDOUT_LOG),
            "stderr": str(run_dir / _ROOT_STDERR_LOG),
            "env": env,
        }
        write_text_atomic(run_dir / "seed.json", json.dumps(seed), mode=0o600, sync_parent=True)
        wire = helper().seed_root(seed)
    except Exception as exc:
        raise RootDriverError(f"root_seed over the helper failed: {exc}") from exc
    deadline = time.monotonic() + _ROOT_READY_TIMEOUT_S
    while time.monotonic() < deadline:
        state = wire.get("state")
        if state == "running":
            return
        if state == "conflict":
            raise RootDriverError(
                f"another ava-root owns {run_dir}; the keeper rests in conflict and will not "
                "spawn (resolve the conflict first)"
            )
        if state == "exited":
            raise RootDriverError(
                f"the keeper stopped restarting a root that keeps exiting ({wire.get('last_exit')})"
            )
        seed_error = wire.get("seed_error")
        if seed_error:
            raise RootDriverError(f"the root keeper rejected the seed: {seed_error}")
        poll_sleep(_READY_POLL_INTERVAL_S)
        try:
            wire = helper().root_status()
        except Exception as exc:
            raise RootDriverError(f"root_status over the helper failed: {exc}") from exc
    raise RootDriverError(
        f"the helper did not bring ava-root up within {_ROOT_READY_TIMEOUT_S:.0f}s "
        f"(keeper state={wire.get('state')})"
    )


def bring_up_root(
    run_dir: Path,
    repo: Path,
    manifests: Path,
    client: Any,
    env: dict[str, str],
    runtime: StartRuntime | None = None,
    *,
    retained_children: list[subprocess.Popen[bytes]],
    helper: HelperFactory,
) -> dict[str, Any]:
    """Start through the required platform owner; never change ownership on failure."""
    if helper_spawn_committed():
        if not helper_wire_ok(helper):
            raise RootDriverError(
                f"the macOS helper is unreachable or lacks {HELPER_PROTOCOLS}; prepare and "
                "activate a reviewed signed helper supporting durable root stop before "
                "starting services. Refusing direct spawning outside the permission ancestry"
            )
        seed_via_helper(run_dir, repo, manifests, env, runtime, helper=helper)
        return await_root_status(client, run_dir)
    proc = spawn_direct(run_dir, repo, manifests, env, runtime, retained_children=retained_children)
    print(f"  + ava-root spawned directly (pid {proc.pid})")
    return await_root_status(client, run_dir, proc=proc)


def changed_units(manifest: dict[str, object], status: dict[str, Any]) -> set[str]:
    """A running root may be reused only for the exact requested launch inputs."""
    from services.supervision.ava_root.manifest import UnitManifest

    units = root_units(status)
    desired = [
        UnitManifest.from_mapping(row, origin="requested tree")
        for row in cast("list[dict[str, object]]", manifest["units"])
    ]
    return {
        item.id
        for item in desired
        if units.get(item.id, {}).get("manifest_digest") != item.digest()
    }


def stop_root_process(
    run_dir: Path, client: Any, status: dict[str, Any], *, timeout_s: float, helper: HelperFactory
) -> None:
    """Stop through the keeper on macOS, or SIGTERM; await native root exit."""
    from base.native_process.root_control.client import native_identity

    require_root_owner(status, helper=helper)
    identity = native_identity(status["root"])
    if helper_spawn_committed():
        if not helper_wire_ok(helper):
            raise RootDriverError(
                "the required macOS helper is unreachable — refusing to "
                "signal a helper-seeded root directly (the keeper would restart it); stop it "
                "through the helper"
            )

        try:
            reply = helper().stop_root()
        except Exception as exc:
            raise RootDriverError(f"root_stop over the helper failed: {exc}") from exc
        if reply.get("stop_requested") is not True:
            raise RootDriverError("helper did not retain root stop intent")
    else:
        identity.send_signal(signal.SIGTERM)
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        alive = identity.live()
        if not alive and root_status(client) is None:
            return
        poll_sleep(_READY_POLL_INTERVAL_S)
    raise RootDriverError(f"ava-root at {run_dir} did not stop within {timeout_s:.0f}s")


def reconcile_units(
    roster: tuple[ServiceSpec, ...],
    client: Any,
    status: dict[str, Any],
    *,
    honor_restart_budget: bool = False,
) -> dict[str, Any]:
    """Resume stopped units within the same immutable root generation.

    This command is an explicit operator start, so it also resumes a unit
    whose recorded intent is stopped: `intent` is the supervisor's policy fact
    and `state` its observed process state (task #4872). The OS boot job's
    retried start is not an operator: with ``honor_restart_budget`` it leaves a
    unit down whose ava-root restart budget is spent.
    """
    units = root_units(status)
    health = cast("dict[str, dict[str, Any]]", status.get("health") or {})
    from base.deploy.progress_timeout import CRITICAL_SERVICE_SESSIONS

    for spec in roster:
        unit = units[spec.session]
        if honor_restart_budget and health.get(spec.session, {}).get("restart_limit_reached"):
            print(f"  · {session_name(spec.session)} spent its restart budget — left down")
            continue
        if unit.get("state") != "running" or unit.get("intent") != "running":
            reason = unit.get("last_error") or unit.get("last_exit") or unit.get("state")
            print(f"  ↑ ava-root unit {session_name(spec.session)} ({reason}) — bringing it up")
            try:
                _call_ok(client.up(spec.session), f"up {spec.session}")
            except RootDriverError as exc:
                if spec.session in CRITICAL_SERVICE_SESSIONS:
                    raise
                print(f"  ✗ optional service {spec.session}: {exc}", file=sys.stderr)
    refreshed = root_status(client)
    return refreshed if refreshed is not None else status


def _classify_units(roster: tuple[ServiceSpec, ...], status: dict[str, Any]) -> LaunchOutcome:
    """The launch verdict: units that should run but could not be spawned."""
    units = root_units(status)
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


def require_same_generation(
    manifest: dict[str, object],
    status: dict[str, Any],
    roster: tuple[ServiceSpec, ...],
    *,
    reconcile: bool,
) -> None:
    changed = changed_units(manifest, status)
    if status["root"].get("launch_digest") != manifest["launch_digest"]:
        changed.add("root launch inputs")
    if reconcile:
        changed |= root_units(status).keys() - {spec.session for spec in roster}
    if changed:
        raise RootDriverError(
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
    helper: HelperFactory,
) -> bool:
    """Observe before any converge/schema write; reuse only identical inputs."""
    from cli.commands.lifecycle.start_generation import launch_digest

    status = root_status(root_client())
    if status is None:
        require_root_absent()
        return False
    require_root_owner(status, helper=helper)
    manifest = tree_manifest(roster, repo, roles=roles)
    from base.paths import ava_home

    manifest["launch_digest"] = launch_digest(repo, root_child_env(), home=ava_home())
    require_same_generation(manifest, status, roster, reconcile=reconcile)
    return True


def ensure_root_service_tree(
    roster: tuple[ServiceSpec, ...],
    repo: Path,
    *,
    roles: MachineRoles,
    reconcile: bool,
    retained_children: list[subprocess.Popen[bytes]],
    helper: HelperFactory,
    runtime: StartRuntime | None = None,
    honor_restart_budget: bool = False,
) -> LaunchOutcome:
    """Reuse an identical generation; changing its inputs requires prior stop."""
    from base.paths import ava_home, root_manifests_path, root_run_dir
    from services.supervision.ava_root_glue.manifests import write_manifest

    run_dir = root_run_dir()
    if runtime is not None:
        runtime.validate()
    env = root_child_env()
    manifest = tree_manifest(roster, repo, roles=roles)
    from cli.commands.lifecycle.start_generation import launch_digest

    manifest["launch_digest"] = launch_digest(repo, env, home=ava_home())
    manifests = root_manifests_path()
    client = root_client()
    try:
        status = root_status(client)
        if status is not None:
            require_root_owner(status, helper=helper)
            require_same_generation(manifest, status, roster, reconcile=reconcile)
        units = root_units(status) if status is not None else {}
        for spec in roster:
            unit = units.get(spec.session)
            if unit is not None and unit.get("state") == "running":
                continue
            if spec.session == "frontend":
                from cli.commands._repo import ensure_frontend_deps

                ensure_frontend_deps(repo)
        if status is None:
            require_root_absent()
            write_manifest(manifests, manifest)
            status = bring_up_root(
                run_dir,
                repo,
                manifests,
                client,
                env,
                runtime,
                retained_children=retained_children,
                helper=helper,
            )
            require_root_owner(status, helper=helper)
            require_same_generation(manifest, status, roster, reconcile=reconcile)
        status = reconcile_units(roster, client, status, honor_restart_budget=honor_restart_budget)
    except RootDriverError as exc:
        print(f"  ✗ ava-root tree bring-up failed: {exc}", file=sys.stderr)
        return LaunchOutcome(roster, tuple(session_name(spec.session) for spec in roster))
    return _classify_units(roster, status)


def start_roster(
    roles: MachineRoles, launch_skip: set[str], *, machine: MachineIdentity
) -> tuple[ServiceSpec, ...]:
    """Return the sole root-owned service roster."""
    return root_tree_roster(roles, launch_skip, machine=machine)


def launch_service_tree(
    roster: tuple[ServiceSpec, ...],
    repo: Path,
    roles: MachineRoles,
    *,
    reconcile: bool,
    retained_children: list[subprocess.Popen[bytes]],
    helper: HelperFactory,
    runtime: StartRuntime | None = None,
    honor_restart_budget: bool = False,
) -> LaunchOutcome:
    """Start the requested services through their root owner."""
    return ensure_root_service_tree(
        roster,
        repo,
        roles=roles,
        reconcile=reconcile,
        retained_children=retained_children,
        helper=helper,
        runtime=runtime,
        honor_restart_budget=honor_restart_budget,
    )


def wait_for_service_tree(
    roster: tuple[ServiceSpec, ...],
    *,
    timeout_s: float,
) -> ReadinessWait:
    """Verify the same root-owned roster after launch."""
    return wait_for_root_services_ready(roster, timeout_s=timeout_s)


def health_verdicts(
    specs: tuple[ServiceSpec, ...], status: dict[str, Any] | None
) -> dict[str, str]:
    """Probe now instead of trusting a cached health result.

    A probe that raises is logged and reads as "unavailable"."""
    if status is None:
        return {}
    verdicts: dict[str, str] = {}
    for spec in specs:
        if spec.identity_probe is None:
            continue
        try:
            verdicts[spec.session] = spec.identity_probe().verdict.value
        except Exception:
            logger.opt(exception=True).warning(
                "identity probe for {} raised; treating it as unavailable", spec.session
            )
            verdicts[spec.session] = "unavailable"
    return verdicts


def unit_ready(unit: dict[str, Any] | None, verdict: str | None) -> bool:
    """Require a running unit and a positive protocol response."""
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
    """Observe the service table and ask the current protocol probes."""
    status = root_status(client)
    units = root_units(status) if status is not None else {}
    verdicts = health_verdicts(specs, status)
    return units, verdicts


def _unready_services(
    specs: tuple[ServiceSpec, ...],
    units: dict[str, dict[str, Any]],
    verdicts: dict[str, str],
) -> tuple[ServiceSpec, ...]:
    return tuple(
        spec
        for spec in specs
        if not unit_ready(units.get(spec.session), verdicts.get(spec.session))
    )


def wait_for_root_services_ready(specs: tuple[ServiceSpec, ...], timeout_s: float) -> ReadinessWait:
    """Wait for core services; optional availability is a diagnostic snapshot."""
    from base.deploy.progress_timeout import CRITICAL_SERVICE_SESSIONS

    client = root_client()
    started_at = time.monotonic()
    deadline = started_at + timeout_s
    critical = tuple(s for s in specs if s.session in CRITICAL_SERVICE_SESSIONS)
    non_critical = tuple(s for s in specs if s.session not in CRITICAL_SERVICE_SESSIONS)
    while True:
        units, verdicts = _fresh_readiness_round(client, specs)
        unready = _unready_services(critical, units, verdicts)
        non_critical_unready = _unready_services(non_critical, units, verdicts)
        now = time.monotonic()
        gone_all = bool(unready) and all(_unit_gone(units.get(s.session)) for s in unready)
        if not unready or gone_all or now >= deadline:
            return ReadinessWait(
                unready,
                now - started_at,
                sessions_gone=gone_all,
                non_critical_unready=non_critical_unready,
            )
        poll_sleep(_READY_POLL_INTERVAL_S)


def require_root_absent() -> None:
    from base.native_process.root_control import instance_lock
    from base.paths import root_run_dir

    run_dir = root_run_dir()
    fd = instance_lock.acquire_instance_lock(run_dir)
    instance_lock.release_instance_lock(fd)


def root_tree_plan(preserve: frozenset[str] = frozenset()) -> list[str]:
    """Display names for the selected exact root units."""
    return sorted(name for name, unit in root_tree_selection().items() if unit not in preserve)


def root_tree_selection() -> dict[str, str]:
    """Map the home's qualified display names to the root's exact unit IDs."""
    status = root_status(root_client())
    if status is None:
        require_root_absent()
        return {}
    return {session_name(unit_id): unit_id for unit_id in root_units(status)}


def stop_dormant_helper_root(deadline: float, *, helper: HelperFactory) -> None:
    """Revoke a pending keeper restart even when no root IPC is serving."""
    if not helper_spawn_committed():
        return
    from base.host.permissions_helper.client import PermissionsHelperError
    from services.desktop.permissions_helper.launchd_job import (
        helper_job_domain,
        helper_job_label,
        retirement_query,
    )

    try:
        keeper = helper().root_status()
    except PermissionsHelperError as exc:
        target = f"{helper_job_domain()}/{helper_job_label()}"
        if retirement_query(target, deadline) is None:
            return
        raise RootDriverError(
            "loaded helper custody is unavailable; stop remains incomplete"
        ) from exc
    if keeper["state"] == "unseeded" and not keeper["seeded"]:
        return
    if keeper.get("pid") is not None or keeper["state"] == "conflict":
        raise RootDriverError("helper owns a root without usable IPC; custody requires recovery")
    if not helper_wire_ok(helper) or helper().stop_root().get("stop_requested") is not True:
        raise RootDriverError("helper did not persist stop of its dormant root")
    require_root_absent()


def stop_root_service_tree(
    *,
    preserve: frozenset[str],
    timeout_s: float = _ROOT_STOP_TIMEOUT_S,
    force: bool = False,
    helper: HelperFactory,
) -> None:
    """Stop the root-owned tree — everything, or only the units not preserved.

    `preserve` holds bare service names (restart's browser, `--keep-service`).
    Stopping every unit also stops the root itself; keeping at least one unit
    leaves the root running to host it. Raises `RootDriverError` when the
    tree cannot be brought down; the caller's phase accounting reports it.
    """
    from base.paths import root_run_dir

    deadline = time.monotonic() + timeout_s
    client = root_client(timeout=timeout_s)
    status = root_status(client)
    if status is None:
        require_root_absent()
        stop_dormant_helper_root(deadline, helper=helper)
        print("  ava-root: no live owner or retained service custody")
        return
    units = root_units(status)
    require_root_owner(status, helper=helper)
    preserved = set(units) & preserve
    stop_ids = sorted(set(units) - preserved)
    if not stop_ids and preserved:
        print("  ava-root: every unit is preserved — tree left running")
        return
    for unit_id in stop_ids:
        remaining_s = deadline - time.monotonic()
        if remaining_s <= 0:
            raise RootDriverError("service stop deadline expired; custody retained")
        client = root_client(timeout=remaining_s)
        response = client.force_down(unit_id) if force else client.down(unit_id)
        _call_ok(response, f"down {unit_id}")
        print(f"  ✓ ava-root unit {session_name(unit_id)} stopped")
    if preserved:
        return
    print(f"  ava-root: stopping the whole tree ({len(stop_ids)} unit(s)) and the root")
    stop_root_process(
        root_run_dir(),
        client,
        status,
        timeout_s=max(0.0, deadline - time.monotonic()),
        helper=helper,
    )
    print("  ✓ ava-root stopped (tree down, root exited)")
