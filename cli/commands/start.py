"""Single start lifecycle: persisted identity, prepared storage, one ready root tree.

Exit zero means the complete admitted roster is ready. Failed readiness never
publishes serving or a known-good version, including during an update or boot.
"""

from __future__ import annotations

import sys
from pathlib import Path

from cli.commands._pause_resume import StartDelegation, resume_after_start
from cli.commands._repo import _repo_root
from cli.commands._setup import _print_missing_setup_error
from cli.commands._start_bookmarks import record_running_sha as _record_running_sha
from cli.commands.migrations import cmd_migrations_apply
from cli.commands.status import cmd_status
from cli.start_runtime import StartRuntime
from ops.service_spec import ServiceSpec
from shared import start_serving
from shared.cluster import session_name
from shared.deploy_timing import SERVICE_READY_TIMEOUT_S
from shared.exit_codes import SERVICES_NOT_READY_EXIT_CODE
from shared.machine import MachineRoles
from shared.paths import prod_service_checkout_error


def _ensure_gateway_data_plane() -> int:
    """Bring up this cluster's data plane — local instance or remote probe.

    The implementation lives in `cli/commands/_data_plane.py` (this module's
    line budget); the wrapper keeps the name tests and callers patch.
    """
    from cli.commands._data_plane import ensure_gateway_data_plane

    return ensure_gateway_data_plane()


def _refuse_occupied_health_ports(roster: tuple[ServiceSpec, ...]) -> int:
    """0 when every health port in `roster` is this unit's to bind, else 1 + why.

    The one thing no port scheme can arrange in advance. A block allocated at
    install keeps two clusters apart, and `--health-port-base` lets an operator
    separate two units by hand — but neither survives a *third* unit appearing
    later, and a WSL2 distro can bind whatever it likes on a loopback Windows
    also reaches. Detection is what does not depend on everyone having agreed
    beforehand, so `ava start` asks the port who is there before it launches
    anything onto it (issue #977).

    Exits 1 rather than the readiness code: nothing has been launched, so this is
    a step that failed, not a host that came up incomplete. That also keeps it
    distinct from incomplete readiness: a port collision refuses the launch
    before any application process is started.

    One occupied port refuses the WHOLE start, gateway and frontend included —
    there is no partial bring-up, because a `.env` is edited once and the whole
    block moves together, so degrading to "start the other six" would leave the
    mixed state the incident was made of. The escape hatch is
    `--disable-service <name>`, which drops the daemon from the roster this gate
    reads and is therefore the way to bring the rest of the unit up while the
    collision is being sorted out; the message says so.
    """
    # Read through the probe owner so the safety fixture guards this lookup.
    import cli.commands._probe as _probe_commands
    from shared.paths import ava_home

    occupied = _probe_commands._occupied_health_ports(roster)
    if not occupied:
        return 0
    print("\n✗ another unit already answers on this unit's daemon health ports:", file=sys.stderr)
    for port in occupied:
        print(f"    {session_name(port.spec.session)}: {port.detail}", file=sys.stderr)
    print(
        "\n  Not starting — NOTHING was launched, including the gateway and the frontend. "
        "Launching onto a held port dies on 'address already in use'; launching onto a "
        "RELAYED one is worse, because the watchdog's probe is answered by the other unit "
        "and the failure reads as green.\n"
        "  A health port belongs to a unit, not to a cluster — two units on one machine "
        "(a second install, or a WSL2 distro whose loopback Windows can reach) need one of "
        "them moved. Give this unit its own block:\n"
        f"      ava start --serve-agent-runner --no-serve-gateway --gateway-url <url> --machine-name <name> --machine-host <host> "
        f"--health-port-base <N>\n"
        f"  or set the AVA_*_HEALTH_PORT keys in {ava_home() / '.env'} directly, then retry "
        "`ava start`.\n"
        "  To bring the rest of this unit up meanwhile, drop the listed daemons from this "
        "start: "
        + " ".join(f"--disable-service {port.spec.session}" for port in occupied)
        + "\n  (that daemon then does not run at all — it is a stopgap, not the fix).",
        file=sys.stderr,
    )
    return 1


def _prepare_start_schema(*, retained: bool) -> int:
    """Verified runtime origin never grants schema migration authority."""
    print("\n→ verify prepared schema" if retained else "\n→ apply pending migrations")
    try:
        if not retained:
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
    runtime: StartRuntime | None = None,
) -> int:
    """Prepare storage/configuration only with no prior live application root."""
    import cli.commands._converge as _converge_commands
    import cli.commands._repo as _repo_commands

    # 1) converge host state (symlink / PATH / $AVA_HOME dirs / plugin config
    # images). Memory initialization is explicit (`ava memory init`). A retained
    # image has already prepared its assets and never runs source convergence.
    retained = runtime is not None and runtime.release is not None
    try:
        if not retained:
            _converge_commands.converge_host(
                repo, roles, services=frozenset(spec.session for spec in roster)
            )
    except Exception as e:
        print(f"  ✗ converge failed: {e}", file=sys.stderr)
        return 1

    # 2) gateway brings up this cluster's own pg/redis instance (under its
    #    $AVA_HOME, on its per-cluster ports); a runner-only host skips (uses the
    #    central node's DB/Redis). macOS: brew binaries via pg_ctl + redis-server;
    #    Linux: pg_ctl + redis-server. No docker on any POSIX platform.
    if "gateway" in roles:
        rc = _ensure_gateway_data_plane()
        if rc != 0:
            return rc
        from cli.commands._data_plane import prepare_gateway_schema

        if not retained:
            prepare_gateway_schema()
    else:
        print("\n→ local services: skipped (agent-runner uses central node's DB/Redis/Milvus)")

    rc = _prepare_start_schema(retained=retained)
    if rc:
        return rc

    if "gateway" in roles:
        from cli.commands._data_plane import complete_gateway_data_plane

        if retained:
            complete_gateway_data_plane(refresh_schema=False)
        else:
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
    from cli.commands._converge_extensions import (
        adopt_local_extensions,
        materialize_cluster_extensions,
    )

    # Adopt first: a name this machine installed before the registry existed is
    # invisible to the materializer until it has a row, and sweeping first means
    # one pass leaves machine and cluster agreeing rather than two.
    if not retained:
        adopt_local_extensions()
        materialize_cluster_extensions()

    return 0


@resume_after_start
def _cmd_start_body(  # noqa: PLR0915 — cohesive linear start sequence (converge -> infra -> services -> status); splitting hurts readability
    machine_name: str | None = None,
    serve_gateway: bool | None = None,  # noqa: FBT001 — tri-state capability flag, always passed by name
    serve_agent_runner: bool | None = None,  # noqa: FBT001 — tri-state capability flag, always passed by name
    serve_observability_station: bool | None = None,  # noqa: FBT001 — tri-state capability flag, always passed by name
    machine_description: str | None = None,
    memory_remote: str | None = None,
    gateway_url: str | None = None,
    disabled_services: tuple[str, ...] = (),
    only_services: tuple[str, ...] = (),
    *,
    all_services: bool = False,
    persist_services: bool = True,
    runtime: StartRuntime | None = None,
) -> int | StartDelegation:
    """Core start logic, shared by cmd_start and cmd_restart.

    Explicit selection is durable when ``persist_services`` is true; omission
    retains prior intent. Internal restarts may add transient exclusions only.
    """
    # Resolve the defining modules at the lifecycle operation boundary.
    import cli.commands._probe as _probe_commands
    import cli.commands._repo as _repo_commands
    import cli.commands._root_driver as _root_driver_commands
    import cli.commands._setup as _setup_commands
    from shared import maintenance

    maintenance.require_start_allowed()
    from shared.paths import ava_home

    if (ava_home() / "destroy-intent.json").exists():
        raise RuntimeError("home is being destroyed or detached; startup refused")

    if runtime is None:
        runtime = StartRuntime.development(_repo_root())
    runtime.validate(ava_home())
    repo = runtime.code_root
    print(f"[ava start] cwd = {repo}")

    # The prod home must not launch from a disposable development checkout.
    err = prod_service_checkout_error(repo) if runtime.release is None else None
    if err:
        print(f"\u2717 {err}", file=sys.stderr)
        return 1

    # 0b) collect & validate setup fields (capability-aware filter)
    args: dict[str, str | bool | None] = {
        "machine_name": machine_name,
        "machine_serve_gateway": serve_gateway,
        "machine_serve_agent_runner": serve_agent_runner,
        "machine_serve_observability_station": serve_observability_station,
        "machine_description": machine_description,
        "memory_remote": memory_remote,
        "gateway_url": gateway_url,
    }
    try:
        resolved, missing = _setup_commands._collect_setup_values(args)
    except ValueError as e:
        # validator failure (e.g. MachineRoleInvalid) — do not persist invalid
        # value, print error + exit.
        print(f"\n✗ {e}", file=sys.stderr)
        return 1
    if missing:
        _print_missing_setup_error(missing, resolved.get("machine_role"))
        return 1

    # reset identity holder so downstream shared.machine.machine_name() /
    # machine_role() see the just-written machine_serve_* files.
    from shared.machine import machine_role, reset_identity

    reset_identity()

    roles = machine_role()
    print(f"\n→ roles = {','.join(sorted(roles))}, machine = {resolved['machine_name']}")
    from shared.platform import raise_fd_limit

    raise_fd_limit(65536)  # every service spawned here inherits the raised ceiling

    # Resolve desired services without publishing changes before admission.
    from cli.commands._repo import _services_for_roles_annotated
    from shared.service_selection import resolve_selection

    names = {spec.session for spec, _reason in _services_for_roles_annotated(roles)}
    launch_skip = resolve_selection(
        names,
        only=only_services,
        excluded=disabled_services,
        all_services=all_services,
        persist=persist_services,
        publish=False,
    )
    roster = _root_driver_commands._start_roster(roles, launch_skip)
    try:
        live = _root_driver_commands.admit_live_start(
            roster, repo, roles, reconcile=persist_services, runtime=runtime
        )
        if not live:
            rc = _prepare_cold_start(
                repo,
                roles,
                roster,
                runtime=runtime,
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
    if live:
        from shared import cluster, db

        cluster.assert_checkpoint_schema_current(db.direct_db_url())

    # 3) UPSERT this host into the machines table. The table is informational
    # for ops (`ava cluster status`) + drives agent-runner self-update orchestration;
    # gateway→agent-runner RPC dials the host's ops URL stored in this row, so
    # a missing/NULL row means the cluster cannot reach it. Failing here means
    # this host is invisible to the cluster — fatal on both roles, because an
    # agent-runner will also fail every subsequent `ava cluster status` and
    # `ava cluster update` orchestration.
    print("\n→ register machine in central DB")
    rc = _repo_commands._register_machine_or_die(resolved, roles)
    if rc != 0:
        return rc

    # 3.5) pure-runner only: probe the gateway over the private network before
    # bringing the host up. A co-located gateway,agent-runner box IS the gateway,
    # so it skips this self-probe. The host reaches the gateway this way for
    # self-heal updates + cluster status; a broken private-network path would only
    # surface later during a self-heal. Catching at start-time means the failure
    # is on this stdout and the host fails non-zero.
    if "agent-runner" in roles and "gateway" not in roles:
        print("\n→ probe gateway")
        rc = _repo_commands._probe_gateway_or_die(resolved["gateway_url"])
        if rc != 0:
            return rc

    # Preparation may materialize plugins on a cold start; publish only now.
    names = {spec.session for spec, _reason in _services_for_roles_annotated(roles)}
    launch_skip = resolve_selection(
        names,
        only=only_services,
        excluded=disabled_services,
        all_services=all_services,
        persist=persist_services,
    )
    roster = _root_driver_commands._start_roster(roles, launch_skip)

    # 4a) probe before binding: refuse to launch a daemon onto a health port
    # another unit already answers on — this is the last point at which nothing
    # has been spawned, and the roster is knowable only after the skip resolves.
    rc = _refuse_occupied_health_ports(roster)
    if rc != 0:
        return rc

    # Failed start attempts must leave recovery actions gated.
    serving_generation = start_serving.begin_start()
    if runtime.release is None:
        _record_running_sha(repo)
    else:
        from shared import running_sha

        assert runtime.source_commit is not None  # noqa: S101 — admitted release invariant
        running_sha.set(runtime.source_commit)
    launch = _root_driver_commands._launch_service_tree(
        roster, repo, roles, reconcile=persist_services, runtime=runtime
    )
    started = launch.started
    # 4a) hand the launch failures to whoever runs this start from another process.
    # Written unconditionally so a clean start clears a previous run's list; the
    # rollout's local leg is the consumer (`update._run_gateway_local_update`),
    # because its `ava start` is a child and an exit code cannot carry names.
    from shared import launch_failures

    launch_failures.record(list(launch.failed))

    # The exact maintenance generation stays held through readiness. Its
    # authorized owner, or resume_after_start, alone may release admission.
    from shared.host_deploy_state import set_posture

    set_posture("paused" if maintenance.held() else "idle")

    # Success requires real readiness for every launched service, frontend included.
    print("\n→ waiting for services to come up")
    wait = _root_driver_commands._wait_for_service_tree(
        tuple(started),
        timeout_s=SERVICE_READY_TIMEOUT_S,
    )

    print("\n→ status")
    cmd_status()

    # 7) gateway reachability hint. The gateway's own .env holds the loopback URL
    # (a box reaches its own gateway over loopback); this prints the OTHER address
    # — what a remote agent-runner dials — so whoever just brought the gateway up
    # can enroll runners against it without hunting for the host/port.
    if any(spec.session == "gateway" for spec in started) and not wait.unready:
        from shared.config import settings as _settings
        from shared.machine import reachable_host
        from shared.netutil import is_loopback_host

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
                f"  join an agent-runner: ava start --serve-agent-runner --no-serve-gateway --gateway-url {reachable} "
                "--machine-name <name> --machine-host <runner-host> "
                "(with AVA_CLUSTER_SECRET set from a non-echoing prompt)"
            )

    # 8) Readiness verdict last: its exit code and printed snapshot describe the same run.
    #
    # Launch failures share the verdict: rollout reads `shared.launch_failures`, while the
    # boot loop retries without an unbounded wait on one service (`shared/boot_policy.py`).
    if launch.failed:
        print(
            f"\n✗ {len(launch.failed)} service(s) could not be launched "
            f": {', '.join(launch.failed)}",
            file=sys.stderr,
        )
    if wait.unready:
        _probe_commands._print_unready_services(wait, SERVICE_READY_TIMEOUT_S)
    # Diagnostic tiers remain visible without weakening the readiness verdict.
    if wait.non_critical_unready:
        _probe_commands._print_non_critical_unready_services(wait.non_critical_unready)
        _probe_commands._notify_non_critical_unready_services(
            wait.non_critical_unready, im_enabled=True
        )
    # The resolved edge: a non-critical service that is up again closes its open
    # alert instance, so the Inspector never keeps showing a resolved failure
    # (QA #1196 P1-1).
    recovered = _probe_commands._recovered_non_critical_specs(started, wait.non_critical_unready)
    if recovered:
        _probe_commands._resolve_recovered_non_critical_alerts(recovered, im_enabled=True)
    if wait.unready or wait.non_critical_unready or launch.failed:
        return SERVICES_NOT_READY_EXIT_CODE

    if not start_serving.mark_serving(serving_generation, runtime=runtime.identity(ava_home())):
        print("  ✗ this start lost its serving generation", file=sys.stderr)
        return 1
    from cli.start_identity import mark_phase
    from shared.paths import ava_home

    mark_phase(ava_home(), "ready")
    return 0


def cmd_start(
    machine_name: str | None = None,
    serve_gateway: bool | None = None,  # noqa: FBT001 — tri-state capability flag, always passed by name
    serve_agent_runner: bool | None = None,  # noqa: FBT001 — tri-state capability flag, always passed by name
    serve_observability_station: bool | None = None,  # noqa: FBT001 — tri-state capability flag, always passed by name
    machine_description: str | None = None,
    memory_remote: str | None = None,
    gateway_url: str | None = None,
    disabled_services: tuple[str, ...] = (),
    only_services: tuple[str, ...] = (),
    *,
    all_services: bool = False,
    persist_services: bool = True,
    runtime: StartRuntime | None = None,
) -> int:
    """Converge one configured unit through storage, schema, root and readiness.

    The public CLI persists first-start identity before reaching this entry.
    Internal restart callers reuse that identity and its durable service selection.
    """
    return _cmd_start_body(
        machine_name=machine_name,
        serve_gateway=serve_gateway,
        serve_agent_runner=serve_agent_runner,
        serve_observability_station=serve_observability_station,
        machine_description=machine_description,
        memory_remote=memory_remote,
        gateway_url=gateway_url,
        disabled_services=disabled_services,
        only_services=only_services,
        all_services=all_services,
        persist_services=persist_services,
        runtime=runtime,
    )
