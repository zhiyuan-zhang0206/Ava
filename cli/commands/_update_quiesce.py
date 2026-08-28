"""
Agent quiesce (stop-the-world) for `ava cluster update` and self-heals.

Split out of `cli/commands/update.py` to keep that module within the file-size
budget. This is the convergence loop that drains every live agent before the
schema migration: signal each live agent to restart (source='system:update',
Redis-woken) and poll until none are left running or the mode's bounded window
elapses. With every restarter paused by the caller (local before Phase A,
remote in Phase A), a quiesced agent stays down until its host comes back on
new code in Phase B. On timeout the stragglers are force-reaped
(`_force_reap_local_agents`: CAS-mark 'restarting' + SIGTERM/SIGKILL) — the
rollout must stay bounded.

`_UPDATE_MODES` ('smooth' / 'force' / 'none') + the timeout derivation
(`_quiesce_timeout_s`) live here with the loop they parameterize. The smooth
window is the configured `AVA_UPDATE_QUIESCE_TIMEOUT_SECONDS` (default 10s,
user ruling 2026-08-26) — deliberately short, so a rollout unblocks the
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
# (default 10s): agents idle or between turns exit at their turn boundary
# inside it; anything still live at the deadline is force-reaped. The window
# is deliberately short (user ruling 2026-08-26) — an agent mid-execute_code
# is cut short and its work lost, accepted in exchange for a fast cluster
# unblock; there is no minimum. Force mode waits only long enough for idle
# agents to drain (~10s) and force-reaps whoever is still live regardless.
_FORCE_QUIESCE_TIMEOUT_S = 10.0

# Update modes: 'smooth' (default) waits the configured short window then
# force-reaps stragglers; 'force' waits ~10s then force-reaps; 'none' skips
# quiesce entirely (the rollout's Phase B, whose agents the gateway-side
# quiesce already drained).
_UPDATE_MODES = ("smooth", "force", "none")


def _quiesce_timeout_s(mode: str) -> float:
    """Quiesce wait for an update mode (see _UPDATE_MODES).

    'smooth' is the configured `update_quiesce_timeout_seconds` (default 10s,
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
    """
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
    """
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
    (source='system:update', Redis-woken), wait per mode, and force-reap
    stragglers on timeout. 'none' (the rollout's Phase B, whose agents the
    gateway-side quiesce already drained) is a no-op returning True.

    With the restarter paused by the caller (spawn_update pauses before the
    updater; the rollout pauses in Phase A), a quiesced agent stays down until
    the host comes back on new code — exactly the rollout contract, now also
    held by standalone self-heals.

    Returns True when every agent quiesced (or none were live).
    """
    import shared.db
    from cli.commands import update as _up_mod
    from shared.machine import machine_name

    if mode == "none":
        return True
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
        f"{len(stragglers)} agent(s) still live — force-reaping",
        file=sys.stderr,
    )
    _up_mod._force_reap_local_agents()
    return False
