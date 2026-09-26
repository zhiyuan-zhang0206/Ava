"""`ava cluster status` — print the full multi-machine roster (thin client).

GETs `/api/cluster/roster` on the gateway, which assembles the
roster server-side: its own row locally + each agent-runner probed in parallel via
the status_probe op. The CLI just renders the returned table.
"""

from __future__ import annotations

import sys
from datetime import datetime

import httpx

from shared.api_contracts.status import MachineStatus
from shared.machine import format_capabilities

_CLUSTER_STATUS_PROBE_TIMEOUT_S = 8.0
# Roster `role` column width: the widest label format_capabilities emits is
# "gateway + agent-runner + observability-station" (44 chars).
_ROLE_COL_W = 44


def cmd_cluster_mark_staging(name: str, *, is_staging: bool) -> int:
    """`ava cluster mark-staging NAME` / `ava cluster unmark-staging NAME`.

    Thin client: POSTs /api/cluster/machines/{name}/staging on the gateway,
    which flips the operator staging flag on the machines row. A staging host
    stays registered + roster-visible but is excluded from the agent-runner
    target set (`list_agent_runners`: the heartbeat probe and cluster
    fan-outs). Exit 1 when the gateway reports no such machine.
    """
    from shared.http_dial import post as dial_post
    from shared.machine import gateway_api_base, gateway_auth_headers

    url = f"{gateway_api_base()}/api/cluster/machines/{name}/staging"
    resp = dial_post(
        url,
        timeout=_CLUSTER_STATUS_PROBE_TIMEOUT_S,
        headers=gateway_auth_headers(),
        json={"is_staging": is_staging},
    )
    if resp.status_code == 404:
        print(f"no machine named {name!r} in the registry", file=sys.stderr)
        return 1
    resp.raise_for_status()
    action = "marked staging" if is_staging else "unmarked staging (now a fan-out target)"
    print(f"{name}: {action}")
    return 0


def cmd_cluster_pause(name: str, *, reason: str | None = None) -> int:
    """`ava cluster pause NAME [--reason ...]` — temporarily pull a machine
    out of the cluster.

    Thin client: POSTs /api/cluster/machines/{name}/pause on the gateway,
    which drains (reassigns in_progress tasks of the machine's agents to
    the drain owner #405, with a note on each), terminates every live agent on
    the machine, resolves any open "machine offline" alert for it and sets the
    pause latch. From then on the machine is hidden from the roster / cluster
    panel / `ava.agents.list_machines()`, is not probed (no offline alerts),
    is skipped by cluster fan-outs and refuses spawns — the cluster shows only
    its active members. The registration row (URL/role) is preserved for resume.
    Exit 1 when the gateway reports no such machine or refuses (own gateway).
    """
    from shared.http_dial import post as dial_post
    from shared.machine import gateway_api_base, gateway_auth_headers

    url = f"{gateway_api_base()}/api/cluster/machines/{name}/pause"
    resp = dial_post(
        url,
        timeout=60.0,
        headers=gateway_auth_headers(),
        json={"reason": reason or ""},
    )
    if resp.status_code == 404:
        print(f"no machine named {name!r} in the registry", file=sys.stderr)
        return 1
    if resp.status_code == 400:
        print(resp.json().get("detail", "pause refused"), file=sys.stderr)
        return 1
    resp.raise_for_status()
    body = resp.json()
    print(
        f"{name}: PAUSED (since {str(body['paused_at'])[:19]})"
        + (f" — {body['pause_reason']}" if body.get("pause_reason") else "")
    )
    print(
        f"  agents terminated via the machine's ops server: {body['terminated_agents']}; "
        f"force-marked in DB (machine unreachable): {body['force_marked_agents']}; "
        f"tasks drained to #405: {body['reassigned_tasks']}"
    )
    print(
        "  hidden from roster/probe/fan-out/spawn until resumed: "
        f"`ava cluster resume {name}` (run on the gateway host)"
    )
    return 0


def cmd_cluster_resume(name: str) -> int:
    """`ava cluster resume NAME` — restore a paused machine as a normal
    cluster member.

    Thin client: POSTs /api/cluster/machines/{name}/resume on the gateway,
    which clears the pause latch; probing, the roster, cluster fan-outs and
    spawn acceptance resume immediately. Exit 1 when the gateway reports no such
    machine. Prints the ops checklist for the machine's own side (it is away,
    and its reachable address may have changed while it was out).
    """
    from shared.http_dial import post as dial_post
    from shared.machine import gateway_api_base, gateway_auth_headers

    url = f"{gateway_api_base()}/api/cluster/machines/{name}/resume"
    resp = dial_post(
        url,
        timeout=_CLUSTER_STATUS_PROBE_TIMEOUT_S,
        headers=gateway_auth_headers(),
        json={},
    )
    if resp.status_code == 404:
        print(f"no machine named {name!r} in the registry", file=sys.stderr)
        return 1
    resp.raise_for_status()
    body = resp.json()
    if body["resumed"]:
        print(f"{name}: resumed — probing / roster / fan-out / spawn restored")
    else:
        print(f"{name}: was not paused (no-op)")
    print(
        f"  machine-side checklist (run ON {name} when it is back online):\n"
        "    1. `ava start` on the machine — register_self refreshes its dial URL "
        "(the reachable address may have changed) and clears its stopped_at latch.\n"
        "    2. If its reachable address changed, the gateway's pg_hba must cover the new IP: "
        "add the machine's new IP/CIDR to the comma-separated AVA_TRUSTED_CIDRS with "
        "`ava config set AVA_TRUSTED_CIDRS=<ranges>`, then run `ava restart` ON THE GATEWAY "
        "HOST — its start leg rewrites pg_hba.conf and reloads the retained Postgres.\n"
        "    3. Respawn the agents that lived on it (pause terminated them); "
        "`ava cluster status` / `ava.agents.list_machines()` shows it again."
    )
    return 0


def cmd_cluster_status() -> int:
    """`ava cluster status` — print the full multi-machine roster.

    Thin client: GET `/api/cluster/roster` on the gateway, which
    assembles the roster server-side (its own row locally + each agent-runner
    probed in parallel via the status_probe op) and returns
    every machine's name / role / paused / live status, plus the cluster-global
    deploy lease stamped per row (the deploy-hold banner). Fails fast on any HTTP
    error rather than masking an unreachable gateway.

    The transport failures get one-line stderr verdicts and a nonzero exit
    instead of an unhandled traceback — the unreachable-machine case is the
    exact situation an operator runs this command for, and a roster probe of a
    down machine can push the gateway's own response past this client's
    timeout budget (#219).
    """
    from shared.http_dial import get as dial_get
    from shared.machine import (
        GatewayApiBaseMissing,
        gateway_api_base,
        gateway_auth_headers,
    )

    try:
        url = f"{gateway_api_base()}/api/cluster/roster"
    except GatewayApiBaseMissing as exc:
        # Same diagnostic posture as `ava status`'s gateway supplement: a host
        # that cannot resolve the gateway URL must still say why.
        print(f"✗ cannot resolve gateway URL: {exc}", file=sys.stderr)
        return 1
    try:
        resp = dial_get(
            url, timeout=_CLUSTER_STATUS_PROBE_TIMEOUT_S, headers=gateway_auth_headers()
        )
    except httpx.TimeoutException as exc:
        print(
            f"✗ gateway at {url} did not respond within "
            f"{_CLUSTER_STATUS_PROBE_TIMEOUT_S:g}s — an unreachable machine's roster "
            f"probe shares the same budget: {exc}",
            file=sys.stderr,
        )
        return 1
    except httpx.TransportError as exc:
        print(f"✗ gateway unreachable at {url}: {exc}", file=sys.stderr)
        return 1
    try:
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        print(
            f"✗ gateway returned HTTP {resp.status_code} for {url}: {exc}",
            file=sys.stderr,
        )
        return 1
    roster = [MachineStatus.model_validate(m) for m in resp.json()]

    if not roster:
        print("(machines table empty — no host has run `ava start` yet)")
        return 0

    for line in _render_roster(roster):
        print(line)
    return 0


def _render_roster(roster: list[MachineStatus]) -> list[str]:
    """Render the decoded /api/cluster/roster payload into aligned text lines
    (the schema and deploy-hold banners above the table, then header +
    separator + one row per machine).

    Pure and split from the HTTP fetch so the row formatting is unit-testable
    against the MachineStatus wire schema, which carries the three capability
    flags (serve_gateway / serve_agent_runner / serve_observability_station)
    and no single `role` field — the role column is derived via
    format_capabilities.
    Assumes a non-empty roster (the caller short-circuits the empty case).
    """
    name_w = max(
        *(len(f"{m.name} (staging)") if m.is_staging else len(m.name) for m in roster),
        len("name"),
    )
    lines = _schema_mismatch_banner(roster) + _hold_banner(roster)
    lines += [
        f"{'name'.ljust(name_w)}  {'role':<{_ROLE_COL_W}} {'paused':<7} {'status':<10} "
        f"{'code':<10} up since",
        "-" * (name_w + 71),
    ]
    for m in roster:
        status = _status_cell(m.online, m.identity_mismatch, m.stopped_at)
        # staging hosts stay roster-visible but are not rollout targets —
        # mark the name so the roster states the exclusion.
        display_name = f"{m.name} (staging)" if m.is_staging else m.name
        paused_str = "?" if m.paused is None else "yes" if m.paused else "no"
        up_since = str(m.up_since_at)[:19] if m.online else "—"
        role = format_capabilities(
            m.serve_gateway, m.serve_agent_runner, m.serve_observability_station
        )
        code_str = _code_cell(m.running_sha, m.head_sha)
        lines.append(
            f"{display_name.ljust(name_w)}  {role:<{_ROLE_COL_W}} {paused_str:<7} {status:<10} "
            f"{code_str:<10} {up_since}"
        )
    return lines


def _schema_mismatch_banner(roster: list[MachineStatus]) -> list[str]:
    """Show schema disagreement or unavailable evidence even while ops is online."""
    lines: list[str] = []
    for machine in roster:
        mismatch = machine.schema_mismatch
        if mismatch is None:
            continue
        lines.append(f"⚠ schema check on {mismatch.machine}: {mismatch.kind}; {mismatch.detail}")
    return lines


def _hold_banner(roster: list[MachineStatus]) -> list[str]:
    """The lines above the table naming the live deploy lease, or none when the
    cluster is free.

    `deploy_hold` is cluster-global and stamped identically on every row, so the
    first row is as good as any — no row is more authoritative than another.

    The banner exists because the refusal it causes happens somewhere else: another
    owner fails to take the deploy lease, and the roster is where an operator looks
    to learn what holds it.

    It carries no "no hold" line: an absent lease is not evidence the cluster is
    free (host-local maintenance takes no cluster lease), so printing "no deploy in
    flight" here would assert more than the roster knows.
    """
    hold = next((m.deploy_hold for m in roster if m.deploy_hold is not None), None)
    if hold is None:
        return []
    return [
        f"deploy hold: {hold}",
        "  while it holds, no other owner can take the cluster deploy lease.",
        "",
    ]


def _status_cell(online: bool, identity_mismatch: bool, stopped_at: datetime | None) -> str:  # noqa: FBT001 — online / identity_mismatch are probe verdicts, passed positionally by the renderer
    """The roster `status` column. `MISMATCH` is a loud third state that outranks
    online/offline: the probe reached an ops server that answered under the WRONG
    machine_name, so this row's gateway_url points at the wrong host — never a
    green 'online'.

    `STALE-STOP` is the fourth: the probe answered AND the row carries a stop
    marker, i.e. the two sources of truth disagree about whether this host exists.
    A plain 'online' here is what hid the 2026-07-28 runner exclusion — the
    roster read healthy while the update fan-out, which filters on that same
    marker, silently dropped the host. The marker is the wrong one (a live probe
    outranks a latch nothing but `ava start` clears), so the cell names the
    contradiction rather than picking a side quietly. The next rollout reconciles
    it (`_resolve_fanout_targets`); an `ava start` on the host clears it outright.
    """
    if identity_mismatch:
        return "MISMATCH"
    if online:
        return "STALE-STOP" if stopped_at else "online"
    return "stopped" if stopped_at else "offline"


def _code_cell(running_sha: str | None, head_sha: str | None) -> str:
    """One cell for the roster `code` column: the commit the live process is
    actually running (`running_sha`), short. `⚠` when it differs from the node's
    checkout HEAD (`head_sha`) — the checkout advanced but the process was not
    restarted, so it is still running stale code (the 2026-07-18 lesson: the
    checkout alone proved nothing; up-since exposed the old process). `—` when
    the answering process froze no commit — it came up outside the supervised
    start path, or its tree is not a git checkout."""
    if running_sha is None:
        return "—"
    short = running_sha[:7]
    if head_sha is not None and running_sha != head_sha:
        return f"⚠ {short}"
    return short


def _fetch_gateway_cluster_status() -> dict[str, object]:
    """GET `/api/cluster/status` and return the decoded ClusterStatus body.

    Used by `ava status`'s gateway-view supplement. Presents the cluster-secret
    bearer (the endpoint is authenticated, like every `/api/cluster/*`) so a
    healthy-but-authed gateway reads as up instead of a false `401 unreachable`.
    Fails fast (`raise_for_status()`) on any HTTP error rather than masking an
    unreachable gateway.
    """
    from shared.http_dial import get as dial_get
    from shared.machine import gateway_api_base, gateway_auth_headers

    url = f"{gateway_api_base()}/api/cluster/status"
    resp = dial_get(url, timeout=10.0, headers=gateway_auth_headers())
    resp.raise_for_status()
    return resp.json()
