"""Single start lifecycle: persisted identity, prepared storage, one ready root tree.

Exit zero means the selected core services are ready. Optional capabilities
can remain unavailable without preventing the host from serving.
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from base.agents.exit_codes import SERVICES_NOT_READY_EXIT_CODE
from base.cluster import session_name
from base.cluster.machine import MachineRoles
from base.deploy.lifecycle import start_serving
from base.deploy.maintenance.pause_owner import PauseOwnerSnapshot
from base.deploy.progress_timeout import SERVICE_READY_TIMEOUT_S
from base.paths import prod_service_checkout_error
from cli.commands._repo import _repo_root
from cli.commands._setup import SetupValues, _missing_setup_message
from cli.commands.lifecycle._pause_resume import StartDelegation, resume_after_start
from cli.commands.lifecycle._start_bookmarks import record_running_sha as _record_running_sha
from cli.commands.lifecycle.migrations import cmd_migrations_apply
from cli.commands.lifecycle.status import cmd_status
from cli.start_runtime import StartRuntime
from ops.roster.service_spec import ServiceSpec


def _ensure_gateway_data_plane(
    *,
    retained_children: list[subprocess.Popen[bytes]] | None = None,
) -> int:
    """Bring up this cluster's data plane — local instance or remote probe.

    The implementation lives in `cli/commands/data_plane/bringup.py` (this module's
    line budget); the wrapper keeps the name tests and callers patch.
    """
    from cli.commands.data_plane.bringup import ensure_gateway_data_plane

    return ensure_gateway_data_plane(retained_children=retained_children)


def _refuse_occupied_health_ports(roster: tuple[ServiceSpec, ...]) -> int:
    """Refuse known core-port conflicts; optional capabilities cannot block start."""
    # Read through the probe owner so the safety fixture guards this lookup.
    import cli.commands._probe as _probe_commands

    occupied = _probe_commands._occupied_health_ports(
        tuple(s for s in roster if s.session in _probe_commands.CRITICAL_SERVICE_SESSIONS)
    )
    if not occupied:
        return 0
    print("\n✗ another unit already answers on this unit's daemon health ports:", file=sys.stderr)
    for port in occupied:
        print(f"    {session_name(port.spec.session)}: {port.detail}", file=sys.stderr)
    print(
        "\n  Not starting — NOTHING was launched, including the gateway and the frontend. "
        "Launching onto a held port dies on 'address already in use'; launching onto a "
        "RELAYED one is worse, because the watchdog's probe is answered by the other home's "
        "daemon and the failure reads as green.\n"
        "  Stop the listed daemon, then retry `ava start`. To bring the rest of this unit "
        "up meanwhile, drop the listed daemons from this start: "
        + " ".join(f"--disable-service {port.spec.session}" for port in occupied)
        + "\n  (that daemon then does not run at all — it is a stopgap, not the fix).",
        file=sys.stderr,
    )
    return 1


def _prepare_start_schema() -> int:
    print("\n→ apply pending migrations")
    try:
        cmd_migrations_apply()
    except Exception as e:
        print(f"  ✗ migrations apply failed: {e}", file=sys.stderr)
        return 1
    return 0


def _prepare_cold_start(
    repo: Path,
    roles: MachineRoles,
    roster: tuple[ServiceSpec, ...],
    *,
    retained_children: list[subprocess.Popen[bytes]] | None = None,
) -> int:
    """Prepare storage/configuration only with no prior live application root."""
    import cli.commands._repo as _repo_commands
    import cli.commands.converge.host as converge_host

    # 1) converge host state (symlink / PATH / $AVA_HOME dirs / plugin config
    # images). Memory initialization is explicit (`ava memory init`).
    try:
        converge_host.converge_host(
            repo, roles, services=frozenset(spec.session for spec in roster)
        )
    except Exception as e:
        print(f"  ✗ converge failed: {e}", file=sys.stderr)
        return 1

    # 2) gateway brings up this cluster's own pg/redis instance (under its
    #    $AVA_HOME, on its recorded ports); a runner-only host skips (uses the
    #    central node's DB/Redis). macOS: brew binaries via pg_ctl + redis-server;
    #    Linux: pg_ctl + redis-server. No docker on any POSIX platform.
    if "gateway" in roles:
        rc = _ensure_gateway_data_plane(retained_children=retained_children)
        if rc != 0:
            return rc
        from cli.commands.data_plane.bringup import prepare_gateway_schema

        prepare_gateway_schema()
    else:
        print("\n→ local services: skipped (agent-runner uses central node's DB/Redis)")

    rc = _prepare_start_schema()
    if rc:
        return rc

    if "gateway" in roles:
        from cli.commands.data_plane.bringup import complete_gateway_data_plane

        complete_gateway_data_plane()
    rc = _repo_commands._assert_schema_current_or_die()
    if rc != 0:
        return rc

    # 2.7) land the cluster's installed extensions on this machine. AFTER the
    # schema check on purpose: converge (step 1) runs before this cluster's
    # Postgres is even up (step 2) and before migrations (step 2.5), so a
    # converge step could not read the registry on a single box at all. Here the
    # data plane is up and known-current, which is the precondition
    # materialization actually has. Reports and continues on failure — a machine
    # that is behind catches up on the next start.
    from cli.commands.extensions.materialize import (
        adopt_local_extensions,
        materialize_cluster_extensions,
    )

    # Adopt first: a name this machine installed before the registry existed is
    # invisible to the materializer until it has a row, and sweeping first means
    # one pass leaves machine and cluster agreeing rather than two.
    adopt_local_extensions()
    materialize_cluster_extensions()

    return 0


@dataclass
class _StartState:
    """What the start phases hand to each other."""

    runtime: StartRuntime
    repo: Path
    resolved: SetupValues
    roles: MachineRoles
    live: bool
    retained_children: list[subprocess.Popen[bytes]]


@dataclass(frozen=True)
class _Selection:
    """The caller's service selection for this start."""

    only_services: tuple[str, ...]
    disabled_services: tuple[str, ...]
    all_services: bool
    persist_services: bool


def _guard_start(
    runtime: StartRuntime | None, operation: PauseOwnerSnapshot | None
) -> tuple[StartRuntime, Path] | int:
    """Refuse a start admission, a destroyed home or a disposable prod checkout."""
    from base.deploy.maintenance import admission

    admission.require_start_allowed(operation)
    from base.paths import ava_home

    if (ava_home() / "destroy-intent.json").exists():
        raise RuntimeError("home is being destroyed or detached; startup refused")

    if runtime is None:
        runtime = StartRuntime.development(_repo_root())
    runtime.validate()
    repo = runtime.code_root
    print(f"[ava start] cwd = {repo}")

    # The prod home must not launch from a disposable development checkout.
    err = prod_service_checkout_error(repo)
    if err:
        print(f"\u2717 {err}", file=sys.stderr)
        return 1
    return runtime, repo


def _resolve_identity() -> tuple[SetupValues, MachineRoles] | int:
    """Collect the home's recorded setup fields and settle its roles against `ava init`."""
    import cli.commands._setup as _setup_commands
    from base.paths import ava_home

    # 0b) collect & validate the home's recorded setup fields (capability-aware filter)
    try:
        resolved, missing = _setup_commands._collect_setup_values()
    except ValueError as e:
        # validator failure (e.g. MachineRoleInvalid) — do not persist invalid
        # value, print error + exit.
        print(f"\n✗ {e}", file=sys.stderr)
        return 1
    if missing:
        print(_missing_setup_message(missing), file=sys.stderr)
        return 1

    # reset identity holder so downstream base.cluster.machine.machine_name() /
    # machine_role() re-resolve from settings.
    from base.cluster.machine import machine_role, reset_identity

    reset_identity()

    roles = machine_role()
    from cli.start_identity import read_intent

    recorded = read_intent(ava_home())
    if recorded is not None and sorted(roles) != recorded["roles"]:
        print(
            f"\n✗ this home's capabilities ({','.join(sorted(roles))}) differ from the ones "
            f"`ava init` recorded ({','.join(recorded['roles'])}); capabilities are fixed when "
            "a home is initialized",
            file=sys.stderr,
        )
        return 1
    print(f"\n→ roles = {','.join(sorted(roles))}, machine = {resolved['machine_name']}")
    from base.native_process.os_platform import raise_fd_limit

    raise_fd_limit(65536)  # every service spawned here inherits the raised ceiling
    return resolved, roles


def _roster_for(
    roles: MachineRoles, selection: _Selection, *, publish: bool = True
) -> tuple[ServiceSpec, ...]:
    """The launch roster of the resolved service selection; `publish=False` only inspects it."""
    import cli.commands.lifecycle.root_driver as _root_driver_commands
    from base.deploy.lifecycle.service_selection import resolve_selection
    from cli.commands._repo import _services_for_roles_annotated

    names = {spec.session for spec, _reason in _services_for_roles_annotated(roles)}
    launch_skip = resolve_selection(
        names,
        only=selection.only_services,
        excluded=selection.disabled_services,
        all_services=selection.all_services,
        persist=selection.persist_services,
        **({} if publish else {"publish": False}),
    )
    return _root_driver_commands.start_roster(roles, launch_skip)


def _admit_start(state: _StartState, selection: _Selection) -> int | None:
    """Admit the start (or prepare a cold one) and check the schema before anything launches."""
    import cli.commands._repo as _repo_commands
    import cli.commands.lifecycle.root_driver as _root_driver_commands
    from base.db import Database

    # Resolve desired services without publishing changes before admission.
    roster = _roster_for(state.roles, selection, publish=False)
    try:
        state.live = _root_driver_commands.admit_live_start(
            roster, state.repo, state.roles, reconcile=selection.persist_services
        )
        if not state.live:
            rc = _prepare_cold_start(
                state.repo, state.roles, roster, retained_children=state.retained_children
            )
            if rc:
                return rc
    except (RuntimeError, OSError, ValueError) as exc:
        print(f"  ✗ start admission/preparation failed: {exc}", file=sys.stderr)
        return 1
    # A live generation may only be inspected here; pending schema changes
    # require a stopped writer boundary and never run as a repeat-start effect.
    rc = _repo_commands._assert_schema_current_or_die()
    if rc:
        return rc
    if state.live:
        from base import cluster

        cluster.assert_checkpoint_schema_current(Database.from_settings().direct_url())
    return None


def _register_and_probe(state: _StartState) -> int | None:
    """Register this host centrally, and (pure runner) probe the gateway before bring-up."""
    import cli.commands._repo as _repo_commands
    from base.db import Database

    # 3) UPSERT this host into the machines table. The table is informational
    # for ops (`ava cluster status`) + drives agent-runner self-update orchestration;
    # gateway→agent-runner RPC dials the host's ops URL stored in this row, so
    # a missing/NULL row means the cluster cannot reach it. Failing here means
    # this host is invisible to the cluster — fatal on both roles, because an
    # agent-runner will also fail every subsequent `ava cluster status` and
    # The fleet update orchestration.
    print("\n→ register machine in central DB")
    rc = _repo_commands._register_machine_or_die(
        Database.from_settings(), state.resolved, state.roles
    )
    if rc != 0:
        return rc

    # 3.5) pure-runner only: probe the gateway over the private network before
    # bringing the host up. A co-located gateway,agent-runner box IS the gateway,
    # so it skips this self-probe. The host reaches the gateway this way for
    # self-heal updates + cluster status; a broken private-network path would only
    # surface later during a self-heal. Catching at start-time means the failure
    # is on this stdout and the host fails non-zero.
    if "agent-runner" in state.roles and "gateway" not in state.roles:
        print("\n→ probe gateway")
        rc = _repo_commands._probe_gateway_or_die(state.resolved["gateway_url"])
        if rc != 0:
            return rc
    return None


def _print_gateway_hint() -> None:
    """Where a remote agent-runner dials this gateway.

    The gateway's own .env holds the loopback URL (a box reaches its own gateway over
    loopback); this prints the OTHER address, so whoever just brought the gateway up can
    enroll runners against it without hunting for the host/port.
    """
    from base.cluster.machine import reachable_host
    from base.config import settings as _settings
    from base.host.net.predicates import is_loopback_host

    port = _settings.gateway.gateway_port
    host = reachable_host()
    if is_loopback_host(host):
        print(
            f"\n→ gateway reachable at http://{host}:{port} (loopback only — set "
            "AVA_MACHINE_HOST to this box's private-network address to enroll remote agent-runners)"
        )
    else:
        reachable = f"http://{host}:{port}"
        print(f"\n→ gateway reachable at {reachable}")
        print(
            f"  join an agent-runner: ava init --serve-agent-runner --no-serve-gateway --gateway-url {reachable} "
            "--machine-name <name> --machine-host <runner-host> --db-capability <bundle>, then ava start "
            "(bundle from `ava cluster db-authority issue-unit` here; AVA_DB_CAPABILITY_KEY "
            "set from a non-echoing prompt)"
        )


def _critical_launch_failures(launch: Any) -> tuple[str, ...]:
    from cli.commands._probe import CRITICAL_SERVICE_SESSIONS

    names = {session_name(name) for name in CRITICAL_SERVICE_SESSIONS}
    return tuple(name for name in launch.failed if name in names)


def _readiness_verdict(launch: Any, wait: Any) -> int | None:
    """Report all failures, but gate serving only on selected core services."""
    import cli.commands._probe as _probe_commands

    if launch.failed:
        print(
            f"\n✗ {len(launch.failed)} service(s) could not be launched "
            f": {', '.join(launch.failed)}",
            file=sys.stderr,
        )
    if wait.unready:
        _probe_commands._print_unready_services(wait, SERVICE_READY_TIMEOUT_S)
    if wait.non_critical_unready:
        _probe_commands._print_non_critical_unready_services(wait.non_critical_unready)
        _probe_commands._report_non_critical_unready_services(wait.non_critical_unready)
    if wait.unready or _critical_launch_failures(launch):
        return SERVICES_NOT_READY_EXIT_CODE
    return None


@resume_after_start
def _cmd_start_body(
    operation: PauseOwnerSnapshot | None,
    disabled_services: tuple[str, ...] = (),
    only_services: tuple[str, ...] = (),
    *,
    all_services: bool = False,
    persist_services: bool = True,
    runtime: StartRuntime | None = None,
    retained_children: list[subprocess.Popen[bytes]] | None = None,
) -> int | StartDelegation:
    """Core start logic, shared by cmd_start and cmd_restart.

    Explicit selection is durable when ``persist_services`` is true; omission
    retains prior intent. Internal restarts may add transient exclusions only.
    """
    if retained_children is None:
        raise ValueError("start requires its caller-owned PostgreSQL child retention")
    # Resolve the defining modules at the lifecycle operation boundary.
    import cli.commands.lifecycle.root_driver as _root_driver_commands
    from base.deploy.maintenance import admission

    guarded = _guard_start(runtime, operation)
    if isinstance(guarded, int):
        return guarded
    runtime, repo = guarded
    identity = _resolve_identity()
    if isinstance(identity, int):
        return identity
    resolved, roles = identity
    state = _StartState(
        runtime, repo, resolved, roles, live=False, retained_children=retained_children
    )
    selection = _Selection(only_services, disabled_services, all_services, persist_services)
    rc = _admit_start(state, selection)
    if rc is not None:
        return rc
    rc = _register_and_probe(state)
    if rc is not None:
        return rc

    # Preparation may materialize plugins on a cold start; publish only now.
    roster = _roster_for(roles, selection)

    # 4a) probe before binding: refuse to launch a daemon onto a health port
    # another unit already answers on — this is the last point at which nothing
    # has been spawned, and the roster is knowable only after the skip resolves.
    rc = _refuse_occupied_health_ports(roster)
    if rc != 0:
        return rc

    # Failed start attempts must leave recovery actions gated.
    serving_generation = start_serving.begin_start()
    _record_running_sha(repo)
    launch = _root_driver_commands._launch_service_tree(
        roster, repo, roles, reconcile=persist_services, runtime=runtime
    )
    started = launch.started
    # 4a) hand the launch failures to whoever runs this start from another process.
    # Written unconditionally so a clean start clears a previous run's list; the
    # rollout's local leg is the consumer (`update._run_gateway_local_update`),
    # because its `ava start` is a child and an exit code cannot carry names.
    from base.deploy.lifecycle import launch_failures

    launch_failures.record(list(_critical_launch_failures(launch)))

    # The exact maintenance generation stays held through readiness. Its
    # authorized owner, or resume_after_start, alone may release admission.
    from base.db import Database
    from base.deploy.state.host_deploy_state import set_posture

    set_posture(Database.from_settings(), "paused" if admission.held() else "idle")

    # Success requires real readiness for every launched service, frontend included.
    print("\n→ waiting for services to come up")
    wait = _root_driver_commands.wait_for_service_tree(
        tuple(started),
        timeout_s=SERVICE_READY_TIMEOUT_S,
    )

    print("\n→ status")
    cmd_status()

    # 7) gateway reachability hint.
    if any(spec.session == "gateway" for spec in started) and not wait.unready:
        _print_gateway_hint()

    # 8) Readiness verdict last: its exit code and printed snapshot describe the same run.
    rc = _readiness_verdict(launch, wait)
    if rc is not None:
        return rc

    if not start_serving.mark_serving(serving_generation, runtime=runtime.identity()):
        print("  ✗ this start lost its serving generation", file=sys.stderr)
        return 1
    from base.paths import ava_home
    from cli.start_identity import mark_phase

    mark_phase(ava_home(), "ready")
    return 0


def cmd_start(
    disabled_services: tuple[str, ...] = (),
    only_services: tuple[str, ...] = (),
    *,
    all_services: bool = False,
    persist_services: bool = True,
    runtime: StartRuntime | None = None,
    operation: PauseOwnerSnapshot | None = None,
    retained_children: list[subprocess.Popen[bytes]] | None = None,
) -> int:
    """Converge one configured unit through storage, schema, root and readiness.

    `ava init` persisted the unit's identity before the public CLI reaches this
    entry. Internal restart callers reuse that identity and its durable service
    selection.
    """
    if retained_children is None:
        raise ValueError("start requires its caller-owned PostgreSQL child retention")
    return _cmd_start_body(
        operation,
        disabled_services=disabled_services,
        only_services=only_services,
        all_services=all_services,
        persist_services=persist_services,
        runtime=runtime,
        retained_children=retained_children,
    )
