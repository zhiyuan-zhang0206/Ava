"""
Agent quiesce (stop-the-world) for `ava cluster update` and self-heals.

Split out of `cli/commands/update.py` to keep that module within the file-size
budget. This is the convergence loop that drains every live agent before the
schema migration: signal each live agent to restart (source='system:update',
Redis-woken) and poll until none are left running or the mode's bounded window
elapses. The quiesce pauses the local restarter itself and a quiesced
agent stays down until its host comes back on new code (the gateway-side
Phase A pauses the remote ones; `pause_local_cluster` below covers the
local one for every entry path). On timeout the loop reports the
stragglers and returns False — the CALLER then force-reaps them in its
own force_reap stage (`_force_reap_local_agents`: CAS-mark 'restarting' +
SIGTERM/SIGKILL), never inside the quiesce window: the configured budget
bounds the drain, and the reap's own SIGTERM grace is a separate stage
so the quiesce stage never overruns the budget (Task #2055).

`_UPDATE_MODES` ('smooth' / 'force' / 'none') + the timeout derivation
(`_quiesce_timeout_s`) live here with the loop they parameterize. The smooth
window is the configured `AVA_UPDATE_QUIESCE_TIMEOUT_SECONDS` (default 5s,
user ruling 2026-09-01) — deliberately short, so a rollout unblocks the
cluster fast at the cost of cutting short any agent mid-execute_code; force
is the same shape with an always-reap backstop.

Re-imported by `cli/commands/update.py` (and re-exported through `cli.commands`)
so `cli.commands(.update)._quiesce_all_agents` / `._quiesce_local_agents` /
`._quiesce_timeout_s` keep resolving for the tests and `_cluster_rollback.py`.

"""

from __future__ import annotations

import sys

_QUIESCE_POLL_INTERVAL_S = 1.0
# Smooth mode's window is the configured AVA_UPDATE_QUIESCE_TIMEOUT_SECONDS
# (default 5s): agents idle or between turns exit at their turn boundary
# inside it; anything still live at the deadline is force-reaped. The window
# is deliberately short (user ruling 2026-09-01 freeze plan targets roughly
# 8s for stop-the-world) — an agent mid-execute_code is cut short and its work
# lost, accepted in exchange for the force-reap backstop and a fast cluster
# unblock; there is no minimum. Force mode waits only long enough for idle
# agents to drain (~10s) and reports whoever is still live — the caller's
# force_reap stage then reaps them (force mode always reaps, matching the
# gateway orchestration's `force_reap = mode == 'force' or not drained`).
_FORCE_QUIESCE_TIMEOUT_S = 10.0

# Update modes: 'smooth' (default) waits the configured short window then
# force-reaps stragglers; 'force' waits ~10s then force-reaps; 'none' skips
# quiesce entirely (the rollout's Phase B, whose agents the gateway-side
# quiesce already drained).
_UPDATE_MODES = ("smooth", "force", "none")


def _quiesce_timeout_s(mode: str) -> float:
    """Quiesce wait for an update mode (see _UPDATE_MODES).

    'smooth' is the configured `update_quiesce_timeout_seconds` (default 5s,
    no minimum — any non-negative value is legal); 'force' is the fixed ~10s
    idle-drain window; 'none' waits 0 (no quiesce at all).
    """
    if mode == "force":
        return _FORCE_QUIESCE_TIMEOUT_S
    if mode == "none":
        return 0.0
    from shared.config import settings

    return settings.gateway.update_quiesce_timeout_seconds


def _quiesce_pass(signalled: set[int]) -> bool:
    """One convergence pass of the quiesce poll: signal any agent that is live
    but was not signalled yet, and report when nobody is live.

    Returns True when every agent is quiesced (the poll loop exits). Mutates
    `signalled` with the late-arriving agents it signalled — already-signalled
    agents are never re-signalled (a signalled agent that has not exited yet is
    mid-turn and will consume its restart at the turn boundary; once it exits,
    the paused restarter keeps it down, so it cannot reappear live).
    """
    import shared.db

    live = set(shared.db.list_live_agent_ids())
    if not live:
        print("  ✓ all agents quiesced")
        return True
    if live - signalled:
        newly = shared.db.signal_live_agents_restart(
            source="system:update", exclude_agent_ids=signalled
        )
        if newly:
            signalled.update(newly)
            print(f"  · signalled {len(newly)} late-arriving agent(s): {sorted(newly)}")
    return False


def _quiesce_all_agents(timeout_s: float) -> bool:
    """Stop-the-world before the schema migration: signal every live agent to
    restart and wait until none are left running.

    Inserts one `restart` inbound (`source='system:update'`) per agent in
    status running/idling, then polls until no live agent remains or
    `timeout_s` elapses. Each signalled agent's claim node marks itself
    restarting and the process exits; with every restarter already paused
    (local before Phase A, remote in Phase A), a quiesced agent stays down
    until its host comes back on new code in Phase B, which respawns it on the
    new code. The trigger itself is not an agent — the detached `ava-rollout`
    session runs `ava cluster update --local` — so the quiesce never signals
    it and it keeps orchestrating while every agent drains (the one-time
    `ava.self.update()` SDK initiator, which held its own restart until a
    restart inbound reached it, was removed 2026-08).

    The poll is a convergence loop, not a passive wait: each pass re-signals
    any agent that is live but was not signalled yet and would otherwise ride
    out the rollout on old code — an agent whose spawn completes mid-quiesce.
    Already-signalled agents are never re-signalled: a
    signalled agent that has not exited yet is mid-turn and will consume its
    restart at the turn boundary; once it exits, the paused restarter keeps it
    down, so it cannot reappear live.

    Reuses the existing `restart` kind (no new inbound kind, no migration). The
    'system:update' source yields a "You have been updated and restarted"
    lifecycle marker once the agent is respawned. Agents that were idle when
    quiesced commit the marker and go straight back to waiting — only agents
    interrupted mid-task spend an LLM turn to resume (claim's restart /
    restart_completed dispatch), so a rollout does not fan out one LLM call
    per idle agent.

    On timeout this logs the straggler agent ids and returns anyway — the
    rollout must stay bounded; one stuck agent writing old-code rows during the
    migration is degraded-but-tolerable, an indefinite hang is not.

    This is orchestration only (poll loop + progress prints + bounded timeout);
    the agents_meta / inbound_messages SQL it drives lives in `shared.db`
    (signal_live_agents_restart / list_live_agent_ids) so the inbound contract
    and the "live = running|idling" definition stay in one place, shared with
    the per-agent restart path.

    Args:
        timeout_s: max seconds to wait for all agents to quiesce.

    Hosted mode is a no-op returning True: hosted rows stay `idling` for life
    (D1 zero-state-write) so the poll has no drain to converge on — it would
    burn its whole window and still report stragglers. The hosted
    stop-the-world is the fleet's agent-host service stops (Phase B per host),
    whose scheduler checkpoints every in-flight turn on SIGTERM; signalling
    here would restart every agent mid-work for nothing. Returning True also
    keeps the orchestration from computing force_reap, which hosted rows must
    never see (no restarter exists to respawn a CAS-marked 'restarting' row).
    """
    from ops.runner_mode import is_hosted

    if is_hosted():
        print(
            "  · hosted runner: agents run inside the agent-host and their rows "
            "stay idling — no per-agent drain to converge on; the fleet's "
            "agent-host service stops are the stop-the-world (skipping the "
            "signal would otherwise restart every agent for nothing)"
        )
        return True
    import shared.db
    from cli.commands import update as _up_mod

    signalled = set(shared.db.signal_live_agents_restart(source="system:update"))
    print(f"  · signalled {len(signalled)} agent(s) to restart")

    deadline = _up_mod.time.monotonic() + timeout_s
    while True:
        if _up_mod._quiesce_pass(signalled):
            return True
        if _up_mod.time.monotonic() >= deadline:
            break
        _up_mod.time.sleep(_QUIESCE_POLL_INTERVAL_S)

    stragglers = shared.db.list_live_agent_ids()
    print(
        f"  ⚠ quiesce timed out after {timeout_s:.0f}s; "
        f"{len(stragglers)} agent(s) still live: {stragglers} — proceeding anyway",
        file=sys.stderr,
    )
    return False


def _force_reap_local_agents(*, defer_process_stop: bool = False) -> list[int]:
    """Force-reap THIS host's live agents: CAS-mark them 'restarting' (so the
    restarter respawns them on new code once the host unpauses) and kill their
    PROCESSES (SIGTERM + bounded wait + SIGKILL). The quiesce-timeout / force-mode
    backstop for agents that could not drain on their own (mid-exec, wedged,
    offline-host stale rows).

    The agents' persistent shell / watcher sessions are deliberately NOT
    reaped (kill_shells=False): they outlive their processes by contract
    (agent/lifecycle.py — persist across terminate/restart/update; each lives
    in its own detached pty host, so the service stop that follows cannot
    reach them either), and an update only wants the agent processes gone so
    the restarter can respawn them on new code. Killing them too is what
    silently killed every watcher on each rollout with a quiesce straggler
    (#1014, 4th recurrence; user ruling 2026-08-08). Only `ava stop` reaps
    shells — that is the "fully stop this cluster" semantic
    (_reap_agent_sessions default).

    `defer_process_stop=True` performs only the CAS marking; `_do_stop` then
    includes those native agent processes in the same batch signal/deadline as
    services. Persistent PTY sessions remain untouched in both forms.

    Returns the ids that were marked (an agent that exited cleanly meanwhile is
    left untouched and its dead process is a noop kill).

    Hosted mode is a no-op returning []: hosted rows stay `idling` for life and
    carry no pid, so "live" is not a straggler signal, and CAS-marking them
    'restarting' would orphan them permanently once the restarter is gated
    out of the hosted roster (PR #1029 — until that gate lands, the
    restarter's hosted branch self-heals a marked row back to idling at the
    cost of one spurious restart_completed per agent, which is why #1029 must
    land before hosted goes live). A hosted force-reap is the one operation
    that can turn a fleet of reachable agents into unreachable ones.
    """
    from ops.runner_mode import is_hosted

    if is_hosted():
        print(
            "  · hosted runner: no per-agent processes to reap — skipping "
            "(CAS-marking rows 'restarting' here would orphan them forever: "
            "the restarter that respawns them is gated out of the hosted roster)"
        )
        return []
    import cli.commands as _ns
    import shared.db
    from shared.machine import machine_name

    live = shared.db.list_live_agent_ids(machine=machine_name())
    if not live:
        return []
    marked = shared.db.mark_agents_restarting(live)
    if marked:
        print(
            f"  · force-reaping {len(marked)} straggler agent(s): {sorted(marked)}",
            file=sys.stderr,
        )
    if not defer_process_stop:
        _ns._reap_agent_sessions(kill_shells=False)
    return marked


def _quiesce_local_agents(mode: str) -> bool:
    """Per-host quiesce for the agent-runner self-update / `ava restart
    --quiesce`: signal THIS machine's live agents to restart
    (source='system:update', Redis-woken) and wait per mode. 'none' (the
    rollout's Phase B, whose agents the gateway-side quiesce already drained)
    is a no-op returning True.

    The quiesce itself pauses the local restarter (idempotent — an owned
    pause, e.g. the updater ladder's spawn-time one, is an unchanged repeat),
    so a signalled agent that exits stays down instead of being respawned
    mid-poll: without the pause every drained agent is instantly respawned on
    old code, the convergence poll can never see an empty live set, and the
    deadline reap would then kill freshly-respawned LIVE agents mid-turn —
    the 2026-08-30 incident (Task #2055), where `ava restart --quiesce --mode
    force` force-reaped 312/1818/3242 seconds after they had drained. The
    caller's trailing `ava start` restores the posture and respawns the
    restarter, so the quiesce never unpauses itself.

    The poll is the whole budget: on timeout the loop reports the stragglers
    and returns False WITHOUT reaping. The caller force-reaps in its own
    force_reap stage (`cmd_restart`, or `_do_stop(force_reap_agents=True)` in
    the self-update) — the reap's own SIGTERM grace window is not quiesce
    time, so the quiesce stage never overruns the mode's budget (the same
    run's quiesce stage took 25.6s = the 10s budget + the reap's 15s window).

    Returns True when every agent quiesced (or none were live).

    Hosted mode is a no-op returning True (and never pauses): agents run
    inside the agent-host and their rows stay `idling` for life, so there are
    no per-agent processes to signal and no drain to poll. The hosted quiesce
    IS the service stop that follows this function — the update flow's
    graceful stop SIGTERMs the agent-host, whose dispatcher unwinds into
    `scheduler.aclose()` and checkpoints every in-flight turn before exit;
    `ava restart`'s non-graceful kill cuts in-flight turns at their last step
    checkpoint instead (the same accepted degradation as a process-mode
    force-reap). Signalling here would insert one restart inbound per agent
    for nothing, and a reap would CAS-mark rows 'restarting' that no
    restarter will ever respawn.
    """
    import shared.db
    from cli.commands import update as _up_mod
    from shared.machine import machine_name

    if mode == "none":
        return True
    from ops.runner_mode import is_hosted

    if is_hosted():
        print(
            "  · hosted runner: agents run inside the agent-host — no per-agent "
            "processes to signal and no drain to poll; the service stop that "
            "follows is the hosted quiesce"
        )
        return True
    # Pause the local restarter BEFORE signalling: the drain contract is that
    # a signalled agent stays down once it exits. An unpaused restarter
    # respawns every drained agent within its ~1s poll, the convergence loop
    # burns the whole budget on agents that already drained, and the deadline
    # reap then kills the freshly-respawned LIVE processes (Task #2055).
    # Idempotent: a caller-owned pause (spawn_update / Phase A) is a harmless
    # repeat, and the caller's trailing `ava start` restores the posture.
    from ops.cluster import pause_local_cluster

    pause_local_cluster()
    timeout_s = _up_mod._quiesce_timeout_s(mode)
    signalled = shared.db.signal_live_agents_restart(source="system:update", machine=machine_name())
    print(f"  · signalled {len(signalled)} local agent(s) to restart")
    if not signalled:
        return True
    deadline = _up_mod.time.monotonic() + timeout_s
    while True:
        live = shared.db.list_live_agent_ids(machine=machine_name())
        if not live:
            print("  ✓ all local agents quiesced")
            return True
        if _up_mod.time.monotonic() >= deadline:
            break
        _up_mod.time.sleep(_QUIESCE_POLL_INTERVAL_S)
    stragglers = shared.db.list_live_agent_ids(machine=machine_name())
    print(
        f"  ⚠ local quiesce timed out after {timeout_s:.0f}s; "
        f"{len(stragglers)} agent(s) still live: {stragglers} — the caller "
        "force-reaps them in its force_reap stage",
        file=sys.stderr,
    )
    return False
