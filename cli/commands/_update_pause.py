"""
Phase A of the gateway `ava cluster update` — pause every restarter.

Split out of `cli/commands/update.py` to keep that module within the file-size
budget. `_stop_the_world` is the pause + quiesce composition that precedes the
schema migration: pause the LOCAL restarter first (so no agent exiting from
here on can be respawned on old code), fan out `cluster/stop` to every remote
agent-runner's ops server (Phase A), then quiesce all agents. A Phase-A 5xx
aborts with nothing migrated (the orchestration's `finally` resumes anyone
paused); `_run_phase_a` returns the set of hosts that ACKED — the answer to
"which hosts did this rollout put into a transition?".

Re-imported by `cli/commands/update.py` (and re-exported through `cli.commands`)
so `cli.commands(.update)._run_phase_a` / `._stop_the_world` keep resolving.

"""

from __future__ import annotations

import sys

from cli.commands._update_fanout import (
    _PHASE_A_TIMEOUT_S,
    ClusterOpPayload,
    _print_fan_out_results,
)


def _run_phase_a(
    agent_runners: list[tuple[str, str | None]],
    *,
    deploy_capability: ClusterOpPayload,
) -> set[str] | None:
    """Phase A: pause every agent-runner. Returns the names that **acked**, or None
    when a host answered 5xx and the rollout must abort (nothing has migrated yet, so
    the cluster can still recover; the caller's `finally` resumes anyone paused).

    The acked set is the answer to "which hosts did this rollout put into a
    transition?", and it is the only such answer available before Phase B hands out
    acks of its own — which is what the gateway-readiness bail-out needs to decide who
    the deploy lease should be held for.
    """
    import cli.commands as _ns

    if not agent_runners:
        print("\n→ Phase A: no agent-runner registered, single-host path")
        return set()
    print(f"\n→ Phase A: pause {len(agent_runners)} agent-runner(s)")
    results = _ns._fan_out(
        agent_runners,
        "/api/cluster/stop",
        _PHASE_A_TIMEOUT_S,
        deploy_capability,
    )
    if _print_fan_out_results("pause", results):
        print(
            "\n✗ Phase A got a 5xx, aborting (schema untouched, cluster can still recover)",
            file=sys.stderr,
        )
        return None
    return {name for name, status, _ in results if status == "ok"}


def _stop_the_world(
    agent_runners: list[tuple[str, str | None]],
    *,
    mode: str = "smooth",
    deploy_capability: ClusterOpPayload,
) -> tuple[set[str] | None, bool]:
    """Pause every restarter (local + remote) and quiesce all agents — the
    stop-the-world that precedes the schema migration.

    Returns (acked_names, all_quiesced): the names Phase A acked — or None when
    Phase A hit a 5xx and the rollout must abort (nothing has migrated yet; the
    caller's compensating `finally` resumes anyone paused) — plus whether every
    agent drained within the mode's quiesce window. `all_quiesced=False` means
    stragglers stayed live (long execs / wedged processes) and the rollout must
    force-reap them on every host.
    """
    import cli.commands as _ns
    from ops.cluster import pause_local_cluster

    # 1) Pause the local restarter FIRST (Phase A only covers remote
    # agent-runners; single-box and the gateway-with-agent-runner case must
    # explicitly pause the local restarter too). Before Phase A, not after:
    # any agent that exits during Phase A's fan-out (which blocks up to
    # _PHASE_A_TIMEOUT_S per unreachable host — 10s bit the 2026-07-13
    # rollout) would otherwise be respawned on old code by the
    # still-running local restarter within its 1s poll. The compensating
    # finally unpauses on every exit path, so pausing early adds no strand
    # risk.
    pause_local_cluster()

    # 1b) Phase A: fan-out cluster/stop. None = a 5xx; abort (the finally resumes
    #     every host we may have paused). Otherwise the set of hosts that acked.
    paused_names = _run_phase_a(agent_runners, deploy_capability=deploy_capability)
    if paused_names is None:
        return None, False

    # 1c) Quiesce all agents before the migration. Every restarter is now
    # paused (local above, remote in Phase A), so signalling each agent to
    # restart takes it down and keeps it down — no old-code agent writes the
    # central DB while the local update migrates the schema. Phase B brings
    # each host back on new code, whose restarter respawns the quiesced
    # agents. The wait is bounded per mode: smooth waits out the longest
    # possible single execute_code (so healthy agents exit at their turn
    # boundary), force waits only for idle agents to drain — either way a
    # timeout means stragglers and the rollout force-reaps them everywhere.
    print("\n→ quiesce all agents before migration (stop-the-world)")
    from cli.commands import update as _up_mod

    all_quiesced = _ns._quiesce_all_agents(timeout_s=_up_mod._quiesce_timeout_s(mode))
    return paused_names, all_quiesced
