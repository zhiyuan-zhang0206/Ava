"""
Fan-out infrastructure for the gateway `ava cluster update` orchestration.

Split out of `cli/commands/update.py` to keep that module within the file-size
budget. This is the parallel-POST machinery that reaches every agent-runner's
ops server, shared by Phase 0's fetch, Phase A's stop, the compensating resume
and Phase B's update:
- `ClusterOpPayload` — the optional params a fan-out cluster op carries.
- `_list_agent_runners` — the fan-out target list (thin wrapper over
  `shared.machines.list_agent_runners()` so tests can monkeypatch it).
- `_PATH_TO_KIND` — fan-out endpoint path -> op kind vocabulary.
- `_dispatch_one_and_wait` / `_fan_out_async` / `_fan_out` — POST one op to one
  host / to every host in parallel / the synchronous CLI-callable wrapper.
- `_print_fan_out_results` — the per-host verdict lines + the has_fatal flag.
- `_PHASE_A_TIMEOUT_S` / `_PHASE_B_TIMEOUT_S` / `_PREFLIGHT_FETCH_TIMEOUT_S` —
  the per-op timeouts.

Re-imported by `cli/commands/update.py` (and re-exported through `cli.commands`)
so `cli.commands(.update)._fan_out` / `._dispatch_one_and_wait` /
`._list_agent_runners` / `._PHASE_*_TIMEOUT_S` keep resolving for tests.

"""

from __future__ import annotations

import asyncio
import sys
from typing import TypedDict

_PHASE_A_TIMEOUT_S = 310.0
_PHASE_B_TIMEOUT_S = 120.0
_PREFLIGHT_FETCH_TIMEOUT_S = 30.0


class ClusterOpPayload(TypedDict, total=False):
    """The optional parameters a fan-out cluster op carries to an agent-runner's
    ops server: `restart_only` (a restart-only bounce vs a full self-update),
    `target_sha` (the pinned rollout commit every node checks out), `mode`
    (the updater's resource-stop policy) and legacy `force_reap` (accepted by
    an older official updater). The validated deploy generation binds pause
    and compensating resume; it is never an agent identity takeover."""

    restart_only: bool
    target_sha: str
    mode: str
    force_reap: bool
    deploy_holder: str
    deploy_acquired_at: str


def _list_agent_runners() -> list[tuple[str, str | None]]:
    """Fan-out target list for this `ava cluster update` — the agent-runners to roll out to.

    Delegates to `shared.machines.list_agent_runners()` (the single
    `SELECT ... FROM machines WHERE role='agent-runner'`); kept as a thin module
    function so the orchestration call sites can monkeypatch it in tests.
    """
    import shared.machines

    return shared.machines.list_agent_runners()


# Mapping from the fan-out endpoint paths to op kinds. `gateway.cluster_rpc.OpKind`
# is the canonical vocabulary of op kinds (and the agent-runner ops server's
# `services/agent_ops/daemon.py:_dispatch` switches on the same set, calling the
# matching `gateway.ops_*` function); the values here must stay a subset of
# it. Not typed `dict[str, OpKind]` on purpose: that would force a top-level
# `gateway.cluster_rpc` import and defeat the lazy-import that keeps non-update
# CLI commands fast. `_fan_out` raises on an unknown path, so a stale entry fails
# loud rather than silently.
_PATH_TO_KIND: dict[str, str] = {
    "/api/cluster/stop": "cluster_stop",
    "/api/cluster/update": "cluster_update",
    "/api/cluster/resume": "cluster_resume",
    "/api/cluster/fetch": "cluster_fetch",
}


async def _dispatch_one_and_wait(
    name: str,
    kind: str,
    timeout_s: float,
    payload: ClusterOpPayload | None = None,
    ops_url: str | None = None,
) -> tuple[str, str, str]:
    """POST one op to target machine's ops server, return (name, status, detail).

    Translates outcomes into the `(ok / unreachable / fatal)` triplet the
    print/abort logic upstream expects. An unreachable ops server (offline /
    mid-restart / not yet registered) is 'unreachable'; an op that ran but
    reported failure is 'fatal'.

    `payload` parameterizes the op (e.g. `{"restart_only": True}` for the
    agent-runner leg of a cluster restart); defaults to an empty dict.

    `ops_url` is the target's pre-resolved ops base URL (from the `machines` read
    the fan-out already did). Threaded so the dial does not re-query Postgres —
    the compensating resume runs when the data plane may be down.
    """
    # Local import — cli/commands/* is loaded by `ava` CLI on every invocation;
    # deferring keeps non-update commands fast.
    from ops import cluster_rpc as cr

    try:
        await cr.dispatch_to_machine(
            target_machine=name,
            kind=kind,  # type: ignore[arg-type]
            payload=dict(payload or {}),
            timeout_s=timeout_s,
            ops_url=ops_url,
        )
    except cr.ClusterOpUnreachable as exc:
        return name, "unreachable", f"ops server unreachable: {exc!s}"
    except cr.ClusterOpFailed as exc:
        return name, "fatal", f"ops-reported failure: {exc.result!r}"
    return name, "ok", f"{kind} ack"


async def _fan_out_async(
    agent_runners: list[tuple[str, str | None]],
    path: str,
    timeout_s: float,
    payload: ClusterOpPayload | None = None,
) -> list[tuple[str, str, str]]:
    """Parallel-POST one op to each agent-runner's ops server, await all results.

    Each tuple's second field is that host's pre-resolved ops URL (from the
    `machines` read `_list_agent_runners` did); it is threaded to the dial so no
    op re-queries Postgres — the compensating resume must dial even after the
    data plane has been stopped by a failed local update.
    """
    # Dynamic lookup so tests can stub `cli.commands._dispatch_one_and_wait`.
    import cli.commands as _ns

    kind = _PATH_TO_KIND.get(path)
    if kind is None:
        raise ValueError(f"unknown fan-out path {path!r} (expected one of {sorted(_PATH_TO_KIND)})")
    tasks = [
        _ns._dispatch_one_and_wait(name, kind, timeout_s, payload, ops_url=url)
        for name, url in agent_runners
    ]
    return list(await asyncio.gather(*tasks))


def _fan_out(
    agent_runners: list[tuple[str, str | None]],
    path: str,
    timeout_s: float,
    payload: ClusterOpPayload | None = None,
) -> list[tuple[str, str, str]]:
    """Synchronous wrapper — runs the async fan-out in its own loop. CLI-callable.

    Each agent-runner gets the op by a direct POST to its ops server, dialed at the
    tuple's pre-resolved ops URL (the `machines`-table read the caller already did),
    so no dial re-queries Postgres — the compensating resume runs when the data
    plane may be down. A None URL there falls back to a per-op `machines` lookup.

    `payload` is forwarded onto each op (e.g. `{"restart_only": True}`).
    """
    return asyncio.run(_fan_out_async(agent_runners, path, timeout_s, payload))


def _print_fan_out_results(label: str, results: list[tuple[str, str, str]]) -> bool:
    """Print fan-out results; returns has_fatal (any 5xx -> True; main flow should abort)."""
    has_fatal = False
    for name, status, detail in results:
        if status == "ok":
            print(f"  ✓ {name}: {label} ack")
        elif status == "unreachable":
            print(f"  ✗ {name}: unreachable; no {label} acknowledgement ({detail})")
        else:
            print(f"  ✗ {name}: {detail}", file=sys.stderr)
            has_fatal = True
    return has_fatal
