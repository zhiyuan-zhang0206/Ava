"""`ava status` — one-screen view of services, infra, host relays, and cron.

Composed from `_print_service_row` (session + probe per spec) +
`print_data_plane_status` (this cluster's own pg/redis) + `print_gate_status`
(the fleet UI entry port) + `print_redis_bridge_status` (authenticated PING
through the private-network relay), followed by the gateway's own cluster-status
snapshot (GET `/api/cluster/status`).

The local session/infra probes are the bootstrap-exemption supplement: they work
even when the gateway is down (e.g. mid-restart), and the gateway can only see
itself. So `ava status` shows both — the local view always, the gateway view
additionally — rather than delegating wholesale.
"""

from __future__ import annotations

from contextlib import suppress

from cli.commands._cluster_instance import print_data_plane_status
from cli.commands._converge_gate import print_gate_status
from cli.commands._converge_redis_bridge import print_redis_bridge_status
from cli.commands._probe import (
    _cluster_pin_status,
    _detect_prod_source_drift,
    _print_service_row,
)
from cli.commands._repo import (
    _repo_root,
    _services_for_roles_annotated,
    build_services,
    session_name,
)
from shared.cluster_drift import prod_source_pin_relation

# Cluster-pin line marks, keyed by `prod_source_pin_relation`. "ahead" means HEAD
# moved past the pin: a stray `git pull`, or a rollout that landed while the pin
# was not advanced. The pin is a floor, not a ceiling — nothing resets the tree
# back to it; `ava cluster update` advances the pin to the live HEAD.
_PIN_MARKS = {
    "aligned": "✓ aligned",
    "behind": "⚠ behind pin (run `ava cluster update`)",
    "ahead": "⚠ ahead of pin — HEAD moved past it (stray `git pull`, or a rollout "
    "that landed without advancing the pin); `ava cluster update` brings the pin up",
    "diverged": "⚠ diverged from pin (run `ava cluster update`)",
    "unknown": "⚠ off pin (run `ava cluster update`)",
}

# What every non-aligned relation means while a cluster update is actually running.
# Mid-rollout the checkout legitimately moves ahead of a pin that is only written
# once the gateway lands the target, so the standing hints accuse an in-flight
# rollout of being a stray `git pull` — on 2026-07-28 that line, read live in a
# rollout log, is what a false alarm was built on. During an update the honest
# reading of any drift is "not converged yet", and none of the remedies apply:
# `ava cluster update` is what is already running.
_PIN_MARK_DURING_UPDATE = "· update in progress — this host has not converged yet"


def _update_in_flight() -> bool:
    """Whether a cluster update owns this cluster right now — a live update-lock
    holder, or an orchestration session alive on this host.

    Both are checked because they cover different hosts: the lock is cluster-wide
    (so an agent-runner sees the gateway's rollout), the session is local (so a
    host running its own `ava cluster update` sees it even though that takes no cluster
    lock). Never raises: this only downgrades an advisory line, so a DB hiccup must
    report "not updating" rather than break `ava status`, the first diagnostic
    command anyone runs when things are already wrong.
    """
    with suppress(Exception):
        from shared.cluster_lock import update_lock_holder

        if update_lock_holder() is not None:
            return True
    try:
        from ops.cluster import current_orchestration

        return current_orchestration() is not None
    except Exception:
        return False


def cmd_status() -> int:
    # Dynamic lookup for monkeypatch-aware tests.
    import cli.commands as _ns

    repo = _repo_root()
    roles = _ns._roles_or_none()
    print(f"[ava status] cwd = {repo}  roles = {','.join(sorted(roles)) if roles else 'unknown'}\n")

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
    name_w = max(len(session_name(spec.session)) for spec, _reason in services_to_show)
    header = f"{'service'.ljust(name_w)}  sess  probe"
    print(header)
    print("-" * len(header))
    for spec, skip_reason in services_to_show:
        _print_service_row(spec, name_w, skip_reason)

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

    # gate section: the fleet UI entry port. Its own section rather than a row in
    # the table above, because the gate is not a session service — it is a launchd
    # KeepAlive job (a pidfile-backed detached process off macOS), so the session
    # column has no answer for it and the two facts worth showing are not the ones
    # a ServiceSpec row carries. Same placement rule as pg/redis: gateway-capable
    # hosts only, since a runner owns no entry port.
    if not runner_only:
        print("\ngate (fleet UI entry):")
        print_gate_status()
        print("\nredis bridge (private-network ingress):")
        print_redis_bridge_status()

    # any installed host: warn if the prod source ($AVA_HOME/source) has drifted
    # off `main`. It is the checkout the live cluster runs from, so it must be
    # reviewed `main` — a feature branch there means the host runs un-reviewed
    # code, and the next rollout force-discards those commits (work in a
    # worktree, never the prod tree).
    if roles:
        drift_branch = _detect_prod_source_drift()
        if drift_branch is not None:
            where = "a detached HEAD" if drift_branch == "HEAD" else f"branch '{drift_branch}'"
            print(
                f"\n⚠ prod source ($AVA_HOME/source) is on {where}, not `main`.\n"
                f"   The live cluster runs this tree — it must be reviewed `main`; a "
                f"feature branch here runs un-reviewed code and the next rollout "
                f"force-discards its commits.\n"
                f"   Develop in a worktree, never the prod checkout. Recover: stash / "
                f"branch any work, then `git -C $AVA_HOME/source checkout main`."
            )

    # any installed host: show the cluster pin (cluster_target_sha — the commit the
    # last rollout pinned the cluster to) vs this host's HEAD, so a node that missed
    # a rollout is visible. Read-only / non-fatal (skipped when no pin yet or the DB
    # is unreachable). The fail-fast-on-drift step is future (commit-pinned-cluster).
    if roles:
        pin_status = _cluster_pin_status()
        if pin_status is not None:
            pin, head = pin_status
            updating = _update_in_flight()
            if head is None:
                head_s = "unknown"
                mark = (
                    _PIN_MARK_DURING_UPDATE
                    if updating
                    else "⚠ HEAD unreadable (run `ava cluster update`)"
                )
            else:
                head_s = head[:7]
                relation = prod_source_pin_relation(pin, head)
                mark = (
                    _PIN_MARK_DURING_UPDATE
                    if updating and relation != "aligned"
                    else _PIN_MARKS[relation]
                )
            print(f"\ncluster pin: {pin[:7]} — this host HEAD {head_s} [{mark}]")

    _print_gateway_cluster_status()
    return 0


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
    from ops.cluster import ClusterStatus
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
    print(f"  paused:       {status.paused}")
