"""Single start lifecycle: persisted identity, prepared storage, one ready root tree.

Exit zero means the complete admitted roster is ready. Failed readiness never
publishes serving or a known-good version, including during an update or boot.
"""

from __future__ import annotations

import sys
from contextlib import nullcontext
from pathlib import Path

from cli.commands._pause_resume import StartDelegation, resume_after_start
from cli.commands._repo import ServiceSpec, _repo_root, session_name
from cli.commands._setup import _print_missing_setup_error
from cli.commands._start_bookmarks import record_running_sha as _record_running_sha
from cli.commands.migrations import cmd_migrations_apply
from cli.commands.status import cmd_status
from shared import start_serving
from shared.deploy_timing import SERVICE_READY_TIMEOUT_S
from shared.exit_codes import SERVICES_NOT_READY_EXIT_CODE
from shared.machine import MachineRoles
from shared.paths import prod_service_checkout_error
from shared.rollout_telemetry import updater_stage
from shared.runtime_migration import ReleaseMigrationContext


def _consume_rollout_parent_handoff() -> bool:
    """Consume the fresh-child marker before any service environment is built."""
    from shared.rollout_handoff import consume_parent_credential_handoff

    return consume_parent_credential_handoff()


def _ensure_gateway_data_plane() -> int:
    """Bring up this cluster's data plane — local instance or remote probe.

    The implementation lives in `cli/commands/_data_plane.py` (this module's
    line budget); the wrapper keeps the name tests and callers patch.
    """
    from cli.commands._data_plane import ensure_gateway_data_plane

    return ensure_gateway_data_plane()


def _rollout_child_window(
    *, parent_handoff: bool, persist_services: bool, gateway_capable: bool
) -> bool:
    """Identify a fresh rollout child, or refuse a concurrent operator start.

    A v1 marker is proof from the surviving parent. An executing DB lease is the
    compatibility signal for a child launched by older code: internal starts
    converge but must defer credential mutation; operator starts are refused.
    Settle holds carry the structured settle fact and have no active orchestrator.
    """
    # Only the gateway local leg survives a checkout in a parent and owns the
    # credential/admission boundary.  A pure agent-runner's Phase-B updater
    # also starts internally under the cluster-wide executing lease, but that
    # child must finish by restoring posture=idle and hosted admission so the
    # gateway's Phase-B poll can observe convergence.
    if parent_handoff and gateway_capable:
        return True

    import shared.db
    from shared.cluster_lock import read_update_lease

    with shared.db.connect(direct=gateway_capable, autocommit=True) as conn:
        lease = read_update_lease(conn=conn)
    if lease is None or lease.is_settle_hold:
        return False
    if persist_services:
        raise RuntimeError(lease.refusal("ava start"))
    return gateway_capable


def _seed_known_good_if_null(roles: frozenset[str]) -> None:
    """Seed the cluster's `last_known_good_sha` to the current HEAD when it has
    never been set — the automatic-rollback floor for a cluster that has not yet
    completed a rollout (otherwise the rollback aborts with "no rollback target").

    Gateway-only: the cluster pin is a single central row, and the gateway's HEAD
    is authoritative for what the cluster runs (an agent-runner's own checkout may
    be at a different sha). Idempotent — seeds exactly once, then no-ops forever.
    Best-effort: a bookkeeping write must not fail an otherwise-successful start."""
    if "gateway" not in roles:
        return
    try:
        from cli.commands._update_git import git_head_sha
        from shared.cluster_pin import seed_last_known_good_sha_if_null

        head = git_head_sha()
        if seed_last_known_good_sha_if_null(head, set_by="seed-on-first-start"):
            print(f"  · seeded last_known_good_sha = {head[:7]} (first successful start)")
    except Exception as exc:
        print(f"  · last_known_good seed skipped ({exc})")


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
    outside `_readiness_waiver` — a rollout must not wave this through, because
    the leg would come up bound to nothing and report success.

    One occupied port refuses the WHOLE start, gateway and frontend included —
    there is no partial bring-up, because a `.env` is edited once and the whole
    block moves together, so degrading to "start the other six" would leave the
    mixed state the incident was made of. The escape hatch is
    `--disable-service <name>`, which drops the daemon from the roster this gate
    reads and is therefore the way to bring the rest of the unit up while the
    collision is being sorted out; the message says so.
    """
    # Via `_ns` so the autouse test guard's monkeypatch of
    # `cli.commands._occupied_health_ports` takes effect at this callsite.
    import cli.commands as _ns
    from shared.paths import ava_home

    occupied = _ns._occupied_health_ports(roster)
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


def _prepare_cold_start(
    repo: Path,
    roles: MachineRoles,
    roster: tuple[ServiceSpec, ...],
    *,
    parent_handoff: bool,
    persist_services: bool,
    updater_telemetry: bool,
    release: ReleaseMigrationContext | None,
) -> tuple[int, bool]:
    """Prepare storage/configuration only with no prior live application root."""
    import cli.commands as _ns

    # 1) converge host state (symlink / PATH / $AVA_HOME dirs / plugin config
    # images). Memory initialization is explicit (`ava memory init`) and never
    # runs during start or rollback. `ava cluster update` inherits this via its
    # trailing cmd_start, so one gateway update converges the whole fleet.
    try:
        _ns.converge_host(repo, roles, services=frozenset(spec.session for spec in roster))
    except Exception as e:
        print(f"  ✗ converge failed: {e}", file=sys.stderr)
        return 1, False

    # 2) gateway brings up this cluster's own pg/redis instance (under its
    #    $AVA_HOME, on its per-cluster ports); a runner-only host skips (uses the
    #    central node's DB/Redis). macOS: brew binaries via pg_ctl + redis-server;
    #    Linux: pg_ctl + redis-server. No docker on any POSIX platform.
    if "gateway" in roles:
        rc = _ensure_gateway_data_plane()
        if rc != 0:
            return rc, False
        from cli.commands._data_plane import prepare_gateway_schema

        prepare_gateway_schema()
    else:
        print("\n→ local services: skipped (agent-runner uses central node's DB/Redis/Milvus)")

    # Detect the process boundary before migrations, grant refresh, or service
    # intent can mutate rollout state. An unreadable lease fails closed.
    try:
        rollout_child = _rollout_child_window(
            parent_handoff=parent_handoff,
            persist_services=persist_services,
            gateway_capable="gateway" in roles,
        )
    except Exception as e:
        print(f"  ✗ cannot start while checking the rollout boundary: {e}", file=sys.stderr)
        return 1, False

    # 2.5) migrations apply (idempotent) — pg is ready; schema must be in place
    # before the service sessions start (gateway connects to agents_meta / register_self
    # writes machines / etc.). On prod restart schema is already applied ->
    # applied 0; first-time bench run -> applies the full set.
    print("\n→ apply pending migrations")
    try:
        with updater_stage("migration") if updater_telemetry else nullcontext():
            (cmd_migrations_apply() if release is None else cmd_migrations_apply(release=release))
    except Exception as e:
        print(f"  ✗ migrations apply failed: {e}", file=sys.stderr)
        return 1, False

    if "gateway" in roles:
        from cli.commands._data_plane import complete_gateway_data_plane

        complete_gateway_data_plane()
    rc = _ns._assert_schema_current_or_die()
    if rc != 0:
        return rc, False

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
    adopt_local_extensions()
    materialize_cluster_extensions()

    return 0, rollout_child


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
    updater_telemetry: bool = False,
    release_receipt: Path | None = None,
) -> int | StartDelegation:
    """Core start logic, shared by cmd_start and cmd_restart.

    Explicit selection is durable when ``persist_services`` is true; omission
    retains prior intent. Internal restarts may add transient exclusions only.
    """
    # Dynamic namespace lookup preserves existing setup/converge/probe test seams.
    import cli.commands as _ns
    from shared import maintenance

    maintenance.require_start_allowed()
    from shared.paths import ava_home

    if (ava_home() / "destroy-intent.json").exists():
        raise RuntimeError("home is being destroyed or detached; startup refused")

    # Receipt admission precedes every setup/converge/source-repair write.
    from cli.commands._release_candidate import admit_start_candidate

    release = admit_start_candidate(release_receipt)

    repo = _repo_root()
    print(f"[ava start] cwd = {repo}")
    parent_handoff = _consume_rollout_parent_handoff()

    # The prod home must not launch from a disposable development checkout.
    err = prod_service_checkout_error(repo)
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
        resolved, missing = _ns._collect_setup_values(args)
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
    roster = _ns._start_roster(roles, launch_skip)
    try:
        live = _ns.admit_live_start(roster, repo, roles, reconcile=persist_services)
        if live:
            rollout_child = _rollout_child_window(
                parent_handoff=parent_handoff,
                persist_services=persist_services,
                gateway_capable="gateway" in roles,
            )
        else:
            rc, rollout_child = _prepare_cold_start(
                repo,
                roles,
                roster,
                parent_handoff=parent_handoff,
                persist_services=persist_services,
                updater_telemetry=updater_telemetry,
                release=release,
            )
            if rc:
                return rc
    except (RuntimeError, OSError, ValueError) as exc:
        print(f"  ✗ start admission/preparation failed: {exc}", file=sys.stderr)
        return 1
    # A live generation may only be inspected here; pending schema changes
    # require a stopped writer boundary and never run as a repeat-start effect.
    rc = _ns._assert_schema_current_or_die()
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
    rc = _ns._register_machine_or_die(resolved, roles)
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
        rc = _ns._probe_gateway_or_die(resolved["gateway_url"])
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
    roster = _ns._start_roster(roles, launch_skip)

    # 4a) probe before binding: refuse to launch a daemon onto a health port
    # another unit already answers on — this is the last point at which nothing
    # has been spawned, and the roster is knowable only after the skip resolves.
    rc = _refuse_occupied_health_ports(roster)
    if rc != 0:
        return rc

    # Failed start attempts must leave recovery actions gated.
    serving_generation = start_serving.begin_start()
    _record_running_sha(repo)
    launch = _ns._launch_service_tree(roster, repo, roles, reconcile=persist_services)
    started = launch.started
    # 4a) hand the launch failures to whoever runs this start from another process.
    # Written unconditionally so a clean start clears a previous run's list; the
    # rollout's local leg is the consumer (`update._run_gateway_local_update`),
    # because its `ava start` is a child and an exit code cannot carry names.
    from shared import launch_failures

    launch_failures.record(list(launch.failed))

    # 5) idempotent clear of the paused state — `ava start` means "I want to
    # serve"; when the gateway crashes between phase A and B leaving
    # a paused posture row so agent-runners are stuck at 503, a manual
    # `ava start` can also recover (no longer requires ssh + rm coordination).
    # R1 (Task #1021): this transition changes only the host posture row. The
    # cluster orchestrator's separate Gate marker spans restart and Phase B;
    # local start must never clear or reclassify that maintenance owner.
    from shared.host_deploy_state import set_posture

    set_posture("paused" if maintenance.held() else "converging" if rollout_child else "idle")

    # Success requires real readiness for every launched service, frontend included.
    print("\n→ waiting for services to come up")
    with updater_stage("readiness") if updater_telemetry else nullcontext():
        wait = _ns._wait_for_service_tree(
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
        _ns._print_unready_services(wait, SERVICE_READY_TIMEOUT_S)
    # Diagnostic tiers remain visible without weakening the readiness verdict.
    if wait.non_critical_unready:
        _ns._print_non_critical_unready_services(wait.non_critical_unready)
        _ns._notify_non_critical_unready_services(wait.non_critical_unready, im_enabled=True)
    # The resolved edge: a non-critical service that is up again closes its open
    # alert instance, so the Inspector never keeps showing a resolved failure
    # (QA #1196 P1-1).
    recovered = _ns._recovered_non_critical_specs(started, wait.non_critical_unready)
    if recovered:
        _ns._resolve_recovered_non_critical_alerts(recovered, im_enabled=True)
    if wait.unready or wait.non_critical_unready or launch.failed:
        return SERVICES_NOT_READY_EXIT_CODE

    from cli.commands._root_driver import complete_boot_start

    try:
        complete_boot_start()
    except (RuntimeError, OSError, TimeoutError) as exc:
        print(f"  ✗ boot manager did not accept the ready root: {exc}", file=sys.stderr)
        return 1

    if not start_serving.mark_serving(serving_generation):
        print("  ✗ this start lost its serving generation", file=sys.stderr)
        return 1
    from cli.start_identity import mark_phase
    from shared.paths import ava_home

    mark_phase(ava_home(), "ready")
    _seed_known_good_if_null(roles)
    if not rollout_child and not maintenance.held():
        # Legacy deploy journals have no continuation hold. Finalize only after
        # readiness; the wrapper releases current maintenance generations.
        from shared.pause_owner import finalize_natural_resume

        finalize_natural_resume()
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
    updater_telemetry: bool = False,
    release_receipt: Path | None = None,
) -> int:
    """Converge one configured unit through storage, schema, root and readiness.

    The public CLI persists first-start identity before reaching this entry.
    Internal restart callers reuse that identity and its durable service selection.
    """
    # Headless runner updates inherit PATH; no tty capture or gate is required.
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
        updater_telemetry=updater_telemetry,
        release_receipt=release_receipt,
    )
