"""`ava status` — one-screen view of services, infra, host relays, and cron.

Composed from `print_service_row` (session + probe per spec) +
`print_data_plane_status` (this cluster's own pg/redis) + `print_redis_bridge_status` (authenticated PING
through the private-network relay), followed by the gateway's own cluster-status
snapshot (GET `/api/cluster/status`).

The local session/infra probes are the bootstrap-exemption supplement: they work
even when the gateway is down (e.g. mid-restart), and the gateway can only see
itself. So `ava status` shows both — the local view always, the gateway view
additionally — rather than delegating wholesale.
"""

from __future__ import annotations

from pathlib import Path

from base.agents.context.clients import DatabaseFactory
from base.cluster.machine import MachineRoles
from base.deploy.git import cluster_drift
from base.deploy.lifecycle import service_selection
from cli.commands._repo import (
    _repo_root,
    _services_for_roles_annotated,
    build_services,
    session_name,
)
from cli.commands.converge.redis_bridge import print_redis_bridge_status
from cli.commands.data_plane.cluster_instance import print_data_plane_status
from cli.commands.lifecycle.hold_report import print_hold_section
from cli.commands.probe import print_service_row
from ops.roster.service_spec import ServiceSpec


def _root_tree_units() -> dict[str, dict[str, object]]:
    """Read one root snapshot; an unreachable root claims no running services."""
    from cli.commands.lifecycle import root_driver

    status = root_driver._root_status(root_driver.root_client())
    if status is None:
        return {}
    return root_driver._root_units(status)


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


def cmd_status(*, database_factory: DatabaseFactory) -> int:
    # Dynamic lookup for monkeypatch-aware tests.
    import cli.commands._repo as _repo_commands

    repo = _repo_root()
    roles = _repo_commands._roles_or_none()
    print(f"[ava status] cwd = {repo}  roles = {','.join(sorted(roles)) if roles else 'unknown'}\n")
    print_hold_section()
    print()
    services_to_show = _status_roster(roles)
    name_w = max((len(session_name(spec.session)) for spec, _reason in services_to_show), default=7)
    # The root is the only service owner. Do not infer liveness from old records.
    root_units = _root_tree_units()
    header = f"{'service'.ljust(name_w)}  root  probe"
    print(header)
    print("-" * len(header))
    for spec, skip_reason in services_to_show:
        print_service_row(spec, name_w, skip_reason, root_units=root_units)

    # infra section: a runner-only host has no local pg/redis.
    runner_only = roles is not None and "agent-runner" in roles and "gateway" not in roles
    if not runner_only:
        print("\ninfra (pg/redis):")
        print_data_plane_status(database_factory=database_factory)
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
    from cli.commands.observability.lgtm import is_lgtm_host, print_lgtm_status

    if is_lgtm_host():
        print("\nlgtm (observability backend):")
        print_lgtm_status()

    # The private-network relay is a host data-plane resource.
    if not runner_only:
        print("\nredis bridge (private-network ingress):")
        print_redis_bridge_status()

    # any installed host: surface the prod source ($AVA_HOME/source) branch state.
    # A source-run home executes that checkout. Source-mode releases materialize
    # as a detached HEAD at the released commit (fleet_update switches every
    # checkout to `--detach NEW`), so a detached tree is the designed steady
    # state — not drift — and `checkout main` would diverge this host from the
    # fleet release. A feature branch here means someone developed in the prod
    # tree instead of a worktree: un-reviewed code on the next restart.
    if roles:
        drift_branch = cluster_drift.prod_source_branch_drift()
        if drift_branch == "HEAD":
            print(
                "\n· prod source ($AVA_HOME/source) is detached at the released commit "
                "(source-mode release state — expected; do not `checkout main`, which "
                "would diverge this host from the fleet release)."
            )
        elif drift_branch is not None:
            print(
                f"\n⚠ prod source ($AVA_HOME/source) is on branch '{drift_branch}', not "
                f"`main`. A source-run home executes this tree; a feature branch here runs "
                f"un-reviewed code on the next restart.\n"
                f"   Develop in a worktree, never the prod checkout. Recover: stash / branch "
                f"any work, then restore this tree to the fleet's released commit (ask the "
                f"operator / re-run the fleet update) — do not `checkout main`."
            )

    # The code this home runs: the source checkout, at its current HEAD.
    print()
    print(_source_identity_line(Path(repo)))

    _print_gateway_cluster_status()
    return 0


def _source_identity_line(repo: Path) -> str:
    """The checkout this home executes; an unreadable HEAD prints as unreadable."""
    from base.deploy.git.cluster_drift import checkout_head_sha

    head = checkout_head_sha(repo)
    return f"release: source checkout {repo} at {head[:7] if head else 'unreadable HEAD'}"


def _print_host_resources() -> None:
    """One live reading, or the reason there is none (psutil absent on this host).

    A read failure prints and returns: the resource line is one section of
    `ava status`, and losing it must not cost the operator the service table
    and the data-plane view below it.
    """
    try:
        from base.host.resource_sample import resource_sample

        s = resource_sample()
    except Exception as e:  # psutil may be absent; the rest of status still prints
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

    from base.cluster.machine import (
        GatewayApiBaseMissing,
        MachineRoleInvalid,
        MachineRoleMissing,
        format_capabilities,
    )
    from cli.commands.cluster.control import fetch_gateway_cluster_status
    from ops.cluster_status import ClusterStatus

    print("\ngateway cluster status (GET /api/cluster/status):")
    try:
        body = fetch_gateway_cluster_status()
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
