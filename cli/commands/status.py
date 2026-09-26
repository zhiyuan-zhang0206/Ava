"""`ava status` — one-screen view of services, infra, host relays, and cron.

Composed from `_print_service_row` (session + probe per spec) +
`print_data_plane_status` (this cluster's own pg/redis) + `print_redis_bridge_status` (authenticated PING
through the private-network relay), followed by the gateway's own cluster-status
snapshot (GET `/api/cluster/status`).

The local session/infra probes are the bootstrap-exemption supplement: they work
even when the gateway is down (e.g. mid-restart), and the gateway can only see
itself. So `ava status` shows both — the local view always, the gateway view
additionally — rather than delegating wholesale.
"""

from __future__ import annotations

from contextlib import suppress
from pathlib import Path

from cli.commands._cluster_instance import print_data_plane_status
from cli.commands._converge_redis_bridge import print_redis_bridge_status
from cli.commands._probe import (
    _detect_prod_source_drift,
    _print_service_row,
)
from cli.commands._repo import (
    _repo_root,
    _services_for_roles_annotated,
    build_services,
    session_name,
)
from ops.service_spec import ServiceSpec
from shared import service_selection
from shared.machine import MachineRoles


def _update_in_flight() -> bool:
    """Whether a live cluster deploy lease is held (package refresh skips then)."""
    with suppress(Exception):
        from shared.cluster_lock import update_lock_holder

        if update_lock_holder() is not None:
            return True
    return False


def _root_tree_units() -> dict[str, dict[str, object]]:
    """Read one root snapshot; an unreachable root claims no running services."""
    from cli.commands import _root_driver

    status = _root_driver._root_status(_root_driver._root_client())
    if status is None:
        return {}
    return _root_driver._root_units(status)


def _status_roster(roles: MachineRoles | None) -> tuple[tuple[ServiceSpec, str | None], ...]:
    """Explain every service excluded by capabilities or the durable desired set."""
    # Show this host's role roster WITH each gated-out service's reason, so a
    # service the start path drops (ava-browser with no display) is visible +
    # explained rather than silently missing — `ava status` is
    # the first diagnostic command and must not go blind on the broken service.
    # Gateway daemons on a runner-only host stay filtered (not in the role union).
    # Unknown role (None) -> show the full list, ungated (no reasons).
    if roles is None:
        services_to_show = tuple((spec, None) for spec in build_services())
    else:
        services_to_show = _services_for_roles_annotated(roles)
    selection = service_selection.read_selection()
    return tuple(
        (spec, reason if selection.enabled(spec.session) else "disabled by desired service set")
        for spec, reason in services_to_show
    )


def cmd_status() -> int:
    # Dynamic lookup for monkeypatch-aware tests.
    import cli.commands._repo as _repo_commands

    repo = _repo_root()
    roles = _repo_commands._roles_or_none()
    print(f"[ava status] cwd = {repo}  roles = {','.join(sorted(roles)) if roles else 'unknown'}\n")
    services_to_show = _status_roster(roles)
    name_w = max((len(session_name(spec.session)) for spec, _reason in services_to_show), default=7)
    # The root is the only service owner. Do not infer liveness from old records.
    root_units = _root_tree_units()
    header = f"{'service'.ljust(name_w)}  root  probe"
    print(header)
    print("-" * len(header))
    for spec, skip_reason in services_to_show:
        _print_service_row(spec, name_w, skip_reason, root_units=root_units)

    # infra section: a runner-only host has no local pg/redis.
    runner_only = roles is not None and "agent-runner" in roles and "gateway" not in roles
    if not runner_only:
        print("\ninfra (pg/redis):")
        print_data_plane_status()
    else:
        print("\ninfra (pg/redis): skipped (agent-runner uses central node)")

    # host section: ONE live CPU / memory / disk reading, read straight from
    # psutil and never from the observability stack — this is precisely the
    # answer that must survive an LGTM backend that is down or was never
    # deployed. The HISTORY lives in Prometheus (issue #46, the Grafana
    # `ava-ops-main` dashboard, "Host & data plane" section); nothing here retains a series.
    print("\nhost (live cpu/memory/disk):")
    _print_host_resources()

    # lgtm section: the observability-backend compose stack (deploy/lgtm) —
    # shown only on the host the operator designated via the $AVA_HOME/lgtm-host
    # marker (a host singleton, not a per-cluster service, so the marker — not
    # the role — decides).
    from cli.commands._lgtm import is_lgtm_host, print_lgtm_status

    if is_lgtm_host():
        print("\nlgtm (observability backend):")
        print_lgtm_status()

    # The private-network relay is a host data-plane resource.
    if not runner_only:
        print("\nredis bridge (private-network ingress):")
        print_redis_bridge_status()

    # any installed host: warn if the prod source ($AVA_HOME/source) has drifted
    # off `main`. A source-run home executes that checkout, so it must be
    # reviewed `main` — a feature branch there means the host runs un-reviewed
    # code (work in a worktree, never the prod tree).
    if roles:
        drift_branch = _detect_prod_source_drift()
        if drift_branch is not None:
            where = "a detached HEAD" if drift_branch == "HEAD" else f"branch '{drift_branch}'"
            print(
                f"\n⚠ prod source ($AVA_HOME/source) is on {where}, not `main`.\n"
                f"   A source-run home executes this tree — it must be reviewed `main`; a "
                f"feature branch here runs un-reviewed code on the next restart.\n"
                f"   Develop in a worktree, never the prod checkout. Recover: stash / "
                f"branch any work, then `git -C $AVA_HOME/source checkout main`."
            )

    # This home's current release identity, from its own durable records: the
    # selected image, or the source checkout it runs, plus any incomplete home
    # operation. There is no cluster-wide pin; the release journal is the record.
    print()
    for line in _release_identity_lines(Path(repo)):
        print(line)

    _print_gateway_cluster_status()
    return 0


def _release_identity_lines(repo: Path) -> list[str]:
    """This home's release identity and any incomplete home operation.

    A selected image (`$AVA_HOME/releases/current-release`) is the release; a
    home without one runs its source checkout. Unreadable records print as
    unreadable — never replaced by a guess or by a historical value.
    """
    from shared.cluster_drift import checkout_head_sha
    from shared.paths import ava_home
    from shared.runtime_release import current_pointer

    home = ava_home()
    try:
        selected = current_pointer(home / "releases")
    except (OSError, ValueError) as exc:
        lines = [f"release: ✗ selector unreadable ({exc})"]
    else:
        if selected is None:
            head = checkout_head_sha(repo)
            lines = [
                f"release: source checkout {repo} at {head[:7] if head else 'unreadable HEAD'}"
            ]
        else:
            artifact, manifest = selected
            lines = [f"release: image {artifact[:12]} (manifest {manifest[:12]})"]
    operation = _home_operation_line(home)
    if operation is not None:
        lines.append(operation)
    return lines


def _home_operation_line(home: Path) -> str | None:
    """The active release/PITR operation unless it completed cleanly."""
    from cli.release_transition.journal import read_operation
    from shared.verified_file import regular_bytes

    try:
        journal = Path(regular_bytes(home / "updates" / "active").decode().strip())
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        return f"  operation: ✗ active pointer unreadable ({exc})"
    try:
        operation = read_operation(journal)
    except (OSError, ValueError) as exc:
        return f"  operation: ✗ journal {journal} unreadable ({exc})"
    if operation.terminal and operation.error is None:
        return None
    request = operation.request
    line = f"  operation: {request.kind} {str(request.id)[:8]} — phase {operation.phase}"
    if operation.direction is not None:
        line += f", direction {operation.direction}"
    if operation.error is not None:
        line += f", error: {operation.error}"
    return line


def _print_host_resources() -> None:
    """One live reading, or the reason there is none (psutil absent on this host).

    A read failure prints and returns: the resource line is one section of
    `ava status`, and losing it must not cost the operator the service table
    and the data-plane view below it.
    """
    try:
        from shared.resource_sample import resource_sample

        s = resource_sample()
    except Exception as e:  # fail-fast-ok: psutil may be absent; the rest of status still prints
        print(f"  unavailable ({e})")
        return
    print(
        f"  cpu {s.cpu_pct:.0f}%   "
        f"memory {s.mem_pct:.0f}% ({s.mem_used_gb:.1f}/{s.mem_total_gb:.1f} GB)   "
        f"disk {s.disk_pct:.0f}% ({s.disk_used_gb:.0f}/{s.disk_total_gb:.0f} GB)"
    )


def _print_gateway_cluster_status() -> None:
    """Fetch + print the gateway's own cluster-status snapshot.

    GET `/api/cluster/status` — this is the thin-client view (what the gateway
    reports about itself: machine_name / capabilities / paused). A gateway that
    is down / unreachable is itself a status signal here, so we catch the HTTP
    error and print it inline rather than aborting the whole `ava status` — the
    local probes above already ran and are the bootstrap-exemption supplement.
    """
    import httpx

    from cli.commands.cluster import _fetch_gateway_cluster_status
    from ops.cluster_status import ClusterStatus
    from shared.machine import (
        GatewayApiBaseMissing,
        MachineRoleInvalid,
        MachineRoleMissing,
        format_capabilities,
    )

    print("\ngateway cluster status (GET /api/cluster/status):")
    try:
        body = _fetch_gateway_cluster_status()
    except httpx.HTTPError as e:
        print(f"  ✗ gateway unreachable: {e}")
        return
    except (GatewayApiBaseMissing, MachineRoleMissing, MachineRoleInvalid) as e:
        # Can't even form the gateway URL (unset gateway URL / role).
        # status is a diagnostic that must still run on a misconfigured host.
        print(f"  ✗ cannot resolve gateway URL: {e}")
        return
    status = ClusterStatus.model_validate(body)
    caps = format_capabilities(
        status.serve_gateway, status.serve_agent_runner, status.serve_observability_station
    )
    print(f"  machine_name: {status.machine_name}")
    print(f"  serves:       {caps}")
    paused_note = f" ({status.paused_reason})" if status.paused_reason else ""
    print(f"  paused:       {status.paused}{paused_note}")
