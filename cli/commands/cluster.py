"""`ava cluster status` — print the full multi-machine roster (thin client).

GETs `/api/cluster/roster` on the gateway, which assembles the
roster server-side: its own row locally + each agent-runner probed in parallel via
the status_probe op. The CLI just renders the returned table.
"""

from __future__ import annotations

import sys
from datetime import datetime
from typing import Any

import httpx

from shared.api_contracts.status import MachineStatus
from shared.last_update import UpdateOutcome
from shared.machine import format_capabilities

_CLUSTER_STATUS_PROBE_TIMEOUT_S = 8.0
# The gateway maps ClusterUpdateInProgress to 409 — a refusal to report, not a
# transport failure to raise on. Every other status still fails fast.
_DEPLOY_REFUSED_STATUS = 409
# Roster `role` column width: the widest label format_capabilities emits is
# "gateway + agent-runner" (22 chars).
_ROLE_COL_W = 22
# Roster `hold` column width: the widest cell is "waited-on" (9 chars).
_HOLD_COL_W = 9


def cmd_cluster_mark_staging(name: str, *, is_staging: bool) -> int:
    """`ava cluster mark-staging NAME` / `ava cluster unmark-staging NAME`.

    Thin client: POSTs /api/cluster/machines/{name}/staging on the gateway,
    which flips the operator staging flag on the machines row. A staging host
    stays registered + roster-visible but is excluded from the rollout target
    set (`list_agent_runners` / the fan-out). Exit 1 when the gateway reports
    no such machine.
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
    action = "marked staging" if is_staging else "unmarked staging (now a rollout target)"
    print(f"{name}: {action}")
    return 0


def cmd_cluster_pause(name: str, *, reason: str | None = None) -> int:
    """`ava cluster pause NAME [--reason ...]` — temporarily pull a machine
    out of the cluster.

    Thin client: POSTs /api/cluster/machines/{name}/pause on the gateway,
    which drains (reassigns open/in_progress tasks of the machine's agents to
    the drain owner #405, with a note on each), terminates every live agent on
    the machine, resolves any open "machine offline" alert for it and sets the
    pause latch. From then on the machine is hidden from the roster / cluster
    panel / `ava.agents.list_machines()`, is not probed (no offline alerts),
    is skipped by rollouts and refuses spawns — the cluster shows only its
    active members. The registration row (URL/role) is preserved for resume.
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
        "  hidden from roster/probe/rollout/spawn until resumed: "
        f"`ava cluster resume {name}` (run on the gateway host)"
    )
    return 0


def cmd_cluster_resume(name: str) -> int:
    """`ava cluster resume NAME` — restore a paused machine as a normal
    cluster member.

    Thin client: POSTs /api/cluster/machines/{name}/resume on the gateway,
    which clears the pause latch; probing, the roster, rollout and spawn
    acceptance resume immediately. Exit 1 when the gateway reports no such
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
        print(f"{name}: resumed — probing / roster / rollout / spawn restored")
    else:
        print(f"{name}: was not paused (no-op)")
    print(
        f"  machine-side checklist (run ON {name} when it is back online):\n"
        "    1. `ava start` on the machine — register_self refreshes its dial URL "
        "(the reachable address may have changed) and clears its stopped_at latch.\n"
        "    2. If its reachable address changed, the gateway's pg_hba must cover the new IP: "
        "on the gateway host set AVA_TRUSTED_CIDRS in ~/.ava/.env to the machine's new "
        "IP/CIDR, then `ava cluster update --restart-only` (regenerates + reloads pg_hba).\n"
        "    3. Respawn the agents that lived on it (pause terminated them); "
        "`ava machines list` / `ava.agents.list_machines()` shows it again."
    )
    return 0


def cmd_cluster_status() -> int:
    """`ava cluster status` — print the full multi-machine roster.

    Thin client: GET `/api/cluster/roster` on the gateway, which
    assembles the roster server-side (its own row locally + each agent-runner
    probed in parallel via the status_probe op) and returns
    every machine's name / role / paused / live status, plus the cluster-global
    deploy lease stamped per row (the `hold` column + its banner). Fails fast on any
    HTTP error rather than masking an unreachable gateway.

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
    (the deploy-hold banner when one is live, then header + separator + one row per
    machine).

    Pure and split from the HTTP fetch so the row formatting is unit-testable
    against the MachineStatus wire schema, which carries the two capability flags
    (serve_gateway / serve_agent_runner) and no single `role` field — the role
    column is derived via format_capabilities. Assumes a non-empty roster (the
    caller short-circuits the empty case).
    """
    name_w = max(
        *(len(f"{m.name} (staging)") if m.is_staging else len(m.name) for m in roster),
        len("name"),
    )
    lines = _last_update_banner(roster) + _hold_banner(roster)
    lines += [
        f"{'name'.ljust(name_w)}  {'role':<{_ROLE_COL_W}} {'paused':<7} {'status':<10} "
        f"{'pin':<10} {'code':<10} {'hold':<{_HOLD_COL_W}} up since",
        "-" * (name_w + 92),
    ]
    for m in roster:
        status = _status_cell(m.online, m.identity_mismatch, m.stopped_at)
        # staging hosts stay roster-visible but are not rollout targets —
        # mark the name so the roster states the exclusion.
        display_name = f"{m.name} (staging)" if m.is_staging else m.name
        paused_str = "?" if m.paused is None else "yes" if m.paused else "no"
        up_since = str(m.up_since_at)[:19] if m.online else "—"
        role = format_capabilities(m.serve_gateway, m.serve_agent_runner)
        pin_str = _pin_cell(m.on_pin, m.head_sha)
        code_str = _code_cell(m.running_sha, m.head_sha)
        hold_str = _hold_cell(m.settle_waited_on)
        lines.append(
            f"{display_name.ljust(name_w)}  {role:<{_ROLE_COL_W}} {paused_str:<7} {status:<10} "
            f"{pin_str:<10} {code_str:<10} {hold_str:<{_HOLD_COL_W}} {up_since}"
        )
    return lines


def _last_update_banner(roster: list[MachineStatus]) -> list[str]:
    """The lines above the table stating that the last update failed, or none when it
    succeeded / is running / was never recorded.

    `last_update` is cluster-global and stamped identically on every row (like the
    pin verdict and the hold), so the first row carrying one is as good as any.

    It goes ABOVE the hold banner because it is the older and more consequential
    fact: a hold explains why the *next* deploy is refused, while this explains what
    the cluster's current state IS. Before it, a failed rollout was legible on this
    roster only as a `pin` / `code` mismatch — and those cells are equally produced
    by a node that missed a rollout and by a checkout that moved without a restart,
    so an operator had to reconstruct which had happened (#1012).

    Silent on success by design. Only a failure changes what an operator does next,
    and a permanent "last update: ok" line would train them to stop reading the top
    of the roster — which is where the failure will be.
    """
    record = next((m.last_update for m in roster if m.last_update is not None), None)
    if record is None or not record.failed:
        return []
    # A recovered update is a failure the cluster already handled, so it gets the
    # warning glyph rather than the failure one: the operator has to READ it, not
    # act on it, and giving both the same mark is how the actionable one stops
    # being read.
    mark = "⚠" if record.outcome is UpdateOutcome.RECOVERED else "✗"
    lines = [f"{mark} {record.describe()}{_age_suffix(record.started_at)}"]
    anchor = next(
        (m.cluster_last_known_good_sha for m in roster if m.cluster_last_known_good_sha), None
    )
    if anchor:
        lines.append(f"  rollback anchor (last known good): {anchor[:7]}")
    if record.pin_advanced:
        lines.append(
            "  the gateway reached the target and the pin advanced, so hosts still off it "
            "converge via their watchdog;"
        )
    else:
        lines.append(
            "  the cluster pin was left where it was, so nothing is converging toward the "
            "failed target;"
        )
    # The rollout recorded the log it was writing, so point at that file. The
    # pattern is the fallback for a record written without one (a foreground
    # `ava cluster update --local`), where naming a specific file would be a guess.
    where = record.log_path or "$AVA_HOME/logs/rollout-<epoch>.log"
    lines += [
        f"  read the rollout's own log on the gateway ({where}). The next successful",
        "  `ava cluster update` replaces this record.",
        "",
    ]
    return lines


def _age_suffix(started_at: datetime | None) -> str:
    """` (Nh ago)` for the failure line — how STALE the failure is changes what an
    operator does with it: minutes old is a live incident, days old is a cluster
    nobody has updated since."""
    if started_at is None:
        return ""
    from datetime import UTC

    seconds = (datetime.now(UTC) - started_at).total_seconds()
    if seconds < 3600:
        return f" ({seconds / 60:.0f}m ago)"
    if seconds < 86400:
        return f" ({seconds / 3600:.0f}h ago)"
    return f" ({seconds / 86400:.0f}d ago)"


def _hold_banner(roster: list[MachineStatus]) -> list[str]:
    """The lines above the table naming the live deploy lease, or none when the
    cluster is free.

    `deploy_hold` is cluster-global and stamped identically on every row (like the
    pin verdict), so the first row is as good as any — no row is more authoritative
    than another.

    The banner exists because a refusal happens somewhere else and later: `ava
    update` refuses, `_assert_no_orchestration_in_flight` says to poll `ava cluster
    status` until every host is on the pin, and the roster then said nothing about
    the hold that was doing the refusing — it was legible only in the health-probe
    cron log or by reading `cluster_update_lock` by hand. It states the operator
    consequence (deploys refused, auto-rollback suppressed) because that is the
    question that brought them here, and it says what the `hold` column is NOT so
    the column is never mistaken for a live convergence check.

    It carries no "no hold" line: a blank `hold` column is not evidence the cluster
    is free (a watchdog-spawned host-local updater takes no lease), so printing
    "no deploy in flight" here would assert more than the roster knows.
    """
    hold = next((m.deploy_hold for m in roster if m.deploy_hold is not None), None)
    if hold is None:
        return []
    return [
        f"deploy hold: {hold}",
        "  while it holds, `ava cluster update` is refused and the health probe's auto-rollback is",
        "  suppressed. `hold` names the hosts the lease RECORDED as still converging when the",
        "  rollout exited — not a live verdict; `pin` / `code` are the live per-host ones.",
        "",
    ]


def _hold_cell(settle_waited_on: bool) -> str:  # noqa: FBT001 — one flag per cell, passed positionally by the renderer like the other cells
    """One cell for the roster `hold` column: whether the live settle hold names this
    host as one it is waiting for.

    Deliberately narrow. This is transcribed from the lease's note — the hosts that
    acked their self-update and were still converging when Phase B gave up — and no
    probe informs it, so the cell means "the hold says it is waiting for this host"
    and nothing more. It is not the deploy-window refusal verdict (that also weighs
    orchestration sessions this roster never probes), and it is not a convergence
    verdict in either direction: `waited-on` does not prove this host is still
    behind, and a blank does not prove it converged — a host that never acked is
    never named by a hold at all. The live reading sits one column left, in `pin` and
    `code`.
    """
    return "waited-on" if settle_waited_on else "—"


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


def _pin_cell(on_pin: bool | None, head_sha: str | None) -> str:  # noqa: FBT001 — on_pin is the tri-state pin verdict, passed positionally by the renderer
    """One cell for the roster `pin` column: ✓/✗ vs the cluster pin plus the
    node's short HEAD. `?` when there is no pin yet or the node's HEAD is unknown
    (on_pin is None) — the same tri-state the gateway computed. This reflects the
    CHECKOUT only; the `code` column reflects the running process."""
    short = head_sha[:7] if head_sha else "—"
    if on_pin is True:
        return f"✓ {short}"
    if on_pin is False:
        return f"✗ {short}"
    return f"? {short}"


def _code_cell(running_sha: str | None, head_sha: str | None) -> str:
    """One cell for the roster `code` column: the commit the live process is
    actually running (`running_sha`), short. `⚠` when it differs from the node's
    checkout HEAD (`head_sha`) — the checkout advanced but the process was not
    restarted, so a node can read `pin ✓` yet still be running stale code (the
    2026-07-18 lesson: pin only proved the checkout; up-since exposed the old
    process). `—` when the answering process froze no commit — it came up
    outside the supervised start path, or its tree is not a git checkout.

    The cell has read this way since it was written; what changed on 2026-07-28
    is that `running_sha` finally means it. It used to be a bookmark file that
    `ava start` rewrote one line before a launcher that skips already-running
    sessions, so the drift this cell exists to show was the one case it could
    not produce."""
    if running_sha is None:
        return "—"
    short = running_sha[:7]
    if head_sha is not None and running_sha != head_sha:
        return f"⚠ {short}"
    return short


def _conflict_detail(resp: httpx.Response) -> str:
    """The refusal text out of a 409 body, or a usable fallback.

    FastAPI puts `str(ClusterUpdateInProgress)` in `detail`. A body that is not the
    expected shape (a proxy's own error page, say) must still produce something an
    operator can act on rather than an empty line or a decode traceback — this runs
    on a path whose entire job is explaining why a deploy was refused.
    """
    try:
        payload = resp.json()
    except ValueError:
        return resp.text.strip() or "a deploy is already in flight (gateway returned 409)"
    payload: dict[str, Any] = payload if isinstance(payload, dict) else {}
    detail = payload.get("detail")
    return str(detail) if detail else "a deploy is already in flight (gateway returned 409)"


def cmd_cluster_restart() -> int:
    """`ava cluster restart` — bounce the whole cluster (thin client).

    POST `/api/cluster/restart` on the gateway: it restarts this
    host and fans out a bounce to every agent-runner, on the current code (no git
    pull / uv sync). The local `ava restart` only touches this host; this is the
    cluster-wide form. Fire-and-forget — the gateway returns the updater session
    immediately; poll `ava cluster status` for the hosts to come back.

    A 409 is the deploy-window refusal (`ops.deploy_window`), not a transport
    failure, so it is reported rather than raised. `raise_for_status()` would throw
    away the one thing the operator needs — the endpoint puts the full refusal, host
    in the way and `--force` hint included, in the response `detail` — and replace it
    with a bare `Client error '409 Conflict' for url ...` traceback. Every other
    status still fails fast.
    """
    from shared.http_dial import post as dial_post
    from shared.machine import gateway_api_base, gateway_auth_headers

    url = f"{gateway_api_base()}/api/cluster/restart"
    print(f"[ava cluster restart] POST {url}")
    resp = dial_post(url, timeout=30.0, headers=gateway_auth_headers())
    if resp.status_code == _DEPLOY_REFUSED_STATUS:
        print(f"\n✗ {_conflict_detail(resp)}", file=sys.stderr)
        return 1
    resp.raise_for_status()
    body = resp.json()
    print(f"  ✓ dispatched: session={body.get('session')} log={body.get('log')}")
    print("  poll `ava cluster status` for hosts to return.")
    return 0


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
