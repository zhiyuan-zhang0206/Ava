"""Agent wake: an EXISTING agents_meta row back into a running process.

The other lifecycle half from `ops/agent_spawn.py` (which creates new rows);
both are reached through `ops/agents.py`. Three wake paths, one shape: a CAS on
`agents_meta.status` into 'allocated' (clearing pid / started_at), an optional
lifecycle inbound committed *before* the launch, then a relaunch attached to the
same `agent_id` — LangGraph's checkpointer restores the message history, so the
process resumes rather than starts over. `resurrect_agent` comes from
'terminated', `respawn_agent` from 'restarting', `swap_in_agent` from
'hibernating'; only the first two write a lifecycle inbound, because
hibernation is deliberately invisible to the agent. The *mechanics* of launching
the child live in `ops/agent_launch.py`, reached via module-qualified access
(`agent_launch._launch_or_force_terminated`).

- **resurrect_agent(agent_id, *, resurrected_by, prompt)** — a
  'terminated' agents_meta row -> 'allocated' + launch process. **Automatically
  INSERTs one kind='resurrect' inbound** (source=resurrected_by, content=''),
  so the agent sees from the LLM perspective that it was resurrected rather
  than receiving an ordinary new message — this is the semantic difference
  between resurrect and spawn. `prompt` is optional: a peer-agent resurrect
  passes one (the reason it woke the other agent); the UI resurrect button is
  a bare lifecycle event and passes none. When given, INSERT another 'chat'
  inbound in the same transaction (kind='chat', content=prompt,
  source=resurrected_by) — **both inbounds are committed before** the process
  launch, eliminating at the root the race "prompt has not arrived yet but
  agent has already started running".
- **respawn_agent(agent_id)** — 'restarting' -> 'allocated' + launch process,
  same pattern as resurrect. **Automatically INSERTs one
  kind='restart_completed' inbound** (source taken from the original 'restart'
  inbound, empty content), used by the restarter daemon, race-safe.

`resurrected_by` is required (no default) — the caller must consciously decide
who triggered the resurrect: SDK path `f"agent:{ava.self.AGENT_ID}"`, gateway HTTP
route reads from body (defaults to 'user'). The value doubles as the inbound
`source` for both the lifecycle row and the optional prompt chat row, so it must
be a legal envelope source (`shared.envelope.validate_source`) — the HTTP layer
enforces this with a 422 (`ResurrectAgentRequest`).
"""

from __future__ import annotations

import json

import shared.db
from ops import agent_launch
from ops.pages import list_open_page_names
from shared.agents import AgentNotFound, AgentStatus, ResurrectAlreadyAlive
from shared.audit_events import insert_event_log
from shared.db import fetch_one
from shared.live_announce import publish_agent_updated_sync, publish_page_closed_sync
from shared.log import logger


def resurrect_agent(
    agent_id: int,
    *,
    resurrected_by: str,
    prompt: str | None = None,
) -> int:
    """Resurrect an already-terminated agent: UPDATE 'terminated' -> 'allocated'
    + INSERT one kind='resurrect' inbound (source=resurrected_by) +
    (optionally) one 'chat' prompt inbound + launch process.

    agent + checkpoints + messages are still in DB; the agent wakes up and
    continues from the last state (LangGraph checkpointer auto-restores
    messages history). The resurrect inbound acts as a lifecycle signal
    picked up by the new process's claim, which appends a lifecycle marker
    "[system ts] You have been resurrected by {resurrected_by}" to messages,
    so the agent from the LLM perspective sees that it was resurrected rather
    than continuing from last turn. This marker is the "ok I'm awake" signal
    and is always present — even when no prompt is delivered.

    `prompt` is optional. When given, INSERT one 'chat' inbound in **the same
    transaction** as the lifecycle 'resurrect' inbound (kind='chat',
    content=prompt, source=resurrected_by) — both inbounds are committed
    before the process launch. This eliminates at the root the race "agent process
    already spawned and running, prompt inbound has not arrived yet"; when
    the agent wakes and SELECTs the batch, both rows are visible, with
    ordering guaranteed by inbound_messages.id (BIGSERIAL monotonic) so
    lifecycle is before chat. When no prompt is given (the UI resurrect
    event), only the lifecycle inbound is written.

    Difference from spawn: spawn does not deliver a lifecycle inbound because
    the agent comes into existence from nothing (no "why was I called"
    question); resurrect always tells the agent that an interrupted
    conversation was externally resumed — otherwise the LLM sees only
    "continue from last time" with no idea that a terminate happened.

    Args:
        agent_id: agent_id of the terminated agent.
        resurrected_by: identifier of the entity triggering the resurrect —
            SDK path `f"agent:{ava.self.AGENT_ID}"`, gateway HTTP route reads from
            body (defaults to 'user'). Written to the inbound source
            field; claim dispatch concatenates into the lifecycle marker so
            the agent knows who resurrected it. Must be a legal envelope
            source — it is reused as the prompt chat inbound's source, and
            an out-of-whitelist value kills the resurrected process on its
            first claim (envelope wrap ValueError).
        prompt: optional follow-up message — when given, delivered as a 'chat'
            inbound committed in the same transaction as the lifecycle
            'resurrect' inbound, so the agent knows why it was resurrected and
            what to do. None (the UI event path) delivers only the marker.

    Returns:
        agent_id (symmetric with create_agent_row).

    Raises:
        AgentNotFound: agent_id does not exist.
        ResurrectAlreadyAlive: agent is in a non-'terminated' state
            (allocated / starting / running / idling / restarting) — any state
            that means "still alive or not fully dead" should not spawn a
            second process.
    """
    with shared.db.connect() as conn, conn.cursor() as cur:
        # SELECT first to distinguish the two raise reasons (AgentNotFound vs
        # ResurrectAlreadyAlive). The race between SELECT and UPDATE is not
        # relied on "WHERE status='terminated'" as a fallback — must
        # explicitly check UPDATE rowcount; 0 rows -> raise: otherwise INSERT
        # would deliver a fake resurrect notification to an agent that is
        # still running.
        cur.execute("SELECT status FROM agents_meta WHERE id = %s", (agent_id,))
        row = cur.fetchone()
        if row is None:
            raise AgentNotFound(f"agent {agent_id} does not exist")
        current = AgentStatus(row[0])
        if current is not AgentStatus.TERMINATED:
            raise ResurrectAlreadyAlive(
                f"agent {agent_id} is in {current.value!r} state, not 'terminated'"
            )
        # Also clear pid + started_at — they are only set during 'running';
        # going back to 'allocated' must reset to NULL, otherwise the
        # previous running session's fields become ghost data (agent 44
        # incident). termination_source is likewise a property of the death we
        # are leaving behind: clear it so the source is strictly per-death — a
        # write site that later forgets to stamp leaves NULL (not resurrectable),
        # which is the whole safety net. last_resurrect_at is NOT cleared here —
        # it is the persistent backoff clock that must span the
        # crash->resurrect->crash cycle, and it is owned by the controller.
        cur.execute(
            "UPDATE agents_meta SET status = %s, pid = NULL, started_at = NULL, "
            "termination_source = NULL, lease_expires_at = NULL "
            "WHERE id = %s AND status = %s",
            (AgentStatus.ALLOCATED, agent_id, AgentStatus.TERMINATED),
        )
        if cur.rowcount != 1:
            # After SELECT, someone else (concurrent resurrect / restart etc.)
            # changed status first — current is no longer 'terminated', we
            # cannot INSERT a resurrect notification + launch a new process
            raise ResurrectAlreadyAlive(
                f"agent {agent_id} was concurrently modified after SELECT; UPDATE affected 0 rows"
            )
        cur.execute(
            "INSERT INTO inbound_messages (agent_id, content, kind, source) "
            "VALUES (%s, '', 'resurrect', %s)",
            (agent_id, resurrected_by),
        )
        if prompt is not None:
            # resurrected_by is itself a legal envelope source (422-validated at
            # the HTTP boundary, ResurrectAgentRequest) — reuse it verbatim as the
            # chat source, no derivation.
            cur.execute(
                "INSERT INTO inbound_messages (agent_id, content, kind, source) "
                "VALUES (%s, %s, 'chat', %s)",
                (agent_id, prompt, resurrected_by),
            )
        cur.execute(
            "SELECT config_overlay, birth_config FROM agents_meta WHERE id = %s", (agent_id,)
        )
        resurrect_overlay: dict[str, object] | None
        resurrect_birth: dict[str, object] | None
        resurrect_overlay, resurrect_birth = fetch_one(cur, "resurrect: read per-agent config")
        # --- lifecycle event ---
        # Parse the resurrector for the directed-edge target (agent:N -> N), same
        # as spawn does for the spawner — so a peer-driven resurrect is a visible
        # inter-agent tie (get_neighbors / FleetView graph), not just a source string.
        resurrect_target: int | None = None
        if resurrected_by.startswith("agent:"):
            import contextlib

            with contextlib.suppress(ValueError):
                resurrect_target = int(resurrected_by.removeprefix("agent:"))
        insert_event_log(
            event_type="resurrect",
            agent_id=agent_id,
            source=resurrected_by,
            target_agent_id=resurrect_target,
            payload={"prompt": prompt} if prompt else {},
        )
        conn.commit()
        publish_agent_updated_sync(conn, agent_id)
    agent_launch._kill_stale_session(agent_id)
    agent_launch._launch_or_force_terminated(
        agent_id, config_overlay=resurrect_overlay, birth_config=resurrect_birth
    )
    logger.info(
        "agent {agent_id} resurrected by {resurrected_by}",
        event="agent_resurrected",
        agent_id=agent_id,
        resurrected_by=resurrected_by,
    )
    return agent_id


def respawn_agent(agent_id: int) -> bool:
    """Called by the restarter daemon: UPDATE 'restarting' -> 'allocated' +
    INSERT one kind='restart_completed' inbound + start a fresh process
    attached to the same agent_id (new PID, LangGraph state preserved).

    Same pattern as `resurrect_agent`: "UPDATE status + INSERT lifecycle
    inbound + launch process". The restart_completed inbound makes the new
    process's claim, on pickup, append "You have been restarted by
    {original_source}" lifecycle marker (source info taken from the
    restart inbounds, with `system:update` preferred over `self:update`
    so the correct "updated and restarted" marker fires when both exist).
    **Race-safe noop** — under concurrent dispatchers, only the UPDATE
    WHERE winner launches the process; others return False and skip.

    The UPDATE commit happens **before** the SELECT/INSERT phase — once
    status='allocated' commit takes effect, restarter no longer polls this
    agent, and a raise in subsequent steps will not trigger infinite retry
    (on raise, agent is stuck at 'allocated', matching launch-failure
    semantics — ops reads logs and cleans up manually).

    Returns:
        True: won the race, new process launched.
        False: another dispatcher won / status changed, noop (does not raise —
            dispatcher should keep polling).
    """
    with shared.db.connect() as conn, conn.cursor() as cur:
        # Phase 1: race-safe gate — UPDATE + commit. Commit makes the
        # restarter see status no longer 'restarting' so it no longer polls
        # this agent. Also clears pid + started_at (restart comes from
        # running, same pattern as resurrect).
        cur.execute(
            "UPDATE agents_meta SET status = %s, pid = NULL, started_at = NULL, "
            "lease_expires_at = NULL WHERE id = %s AND status = %s",
            (AgentStatus.ALLOCATED, agent_id, AgentStatus.RESTARTING),
        )
        won_race = cur.rowcount == 1
        conn.commit()
        if not won_race:
            return False
        logger.info(
            "agent {agent_id} respawn won race, committing phase 1 (restarting -> allocated)",
            event="respawn_phase1",
            agent_id=agent_id,
        )

        # Phase 2: find the original 'restart' inbound's source + content + payload, INSERT 'restart_completed'.
        # Status 'restarting' implies an earlier 'restart' inbound triggered
        # the claim to switch status to 'restarting' (the claim_node case
        # "restart" branch only marks RESTARTING after receiving a restart
        # inbound). row is None = DB integrity violation (ops manually
        # UPDATEd bypassing the inbound path / migration bug); raise to make
        # it visible to ops. At this point status is 'allocated', restarter
        # will not retrigger (raise once and stop).
        #
        # Source passthrough: `ava.self.restart()` posts empty content with
        # source='self' (the removed `ava.self.update()` initiator used
        # 'self:update' — historical rows may still carry it); 'system:update'
        # is a cluster rollout quiesce. The claim node dispatches on that source
        # to render either "restarted by X" or "updated and restarted" marker.
        # The SELECT takes the NEWEST restart row (id DESC) — an older
        # system:update row must not shadow a newer user/self restart, which
        # would render the wrong marker wording (2026-08-08 audit, P3-2).
        # Payload passthrough (PR-E):
        # `ava.self.restart(config_overlay={...})` posts `{"config_overlay": {...}}`;
        # when the new process boots, argparse receives --config-overlay and
        # applies, the restart_completed row is freshly written with an
        # effective_config snapshot by the claim node inside the new process
        # after writing the marker (the restart_completed row this function
        # inserts has payload containing only config_overlay; the new
        # process fills in effective_config after coming up; see
        # agent/loop.py).
        cur.execute(
            "SELECT source, content, payload FROM inbound_messages "
            "WHERE agent_id = %s AND kind = 'restart' "
            "ORDER BY id DESC "
            "LIMIT 1",
            (agent_id,),
        )
        row = cur.fetchone()
        if row is None:
            # DB integrity violated; first mark status 'terminated' and
            # commit so ops can see + caller can resurrect to retry, then
            # raise so the stack trace is visible.
            #
            # termination_source='integrity' (stamped in the same statement, per
            # the invariant in ops/controllers/resurrect.py): a framework-detected
            # inconsistency in the row's own state, which is none of the other four
            # sources — nobody requested this death, no reaper found a dead pid,
            # and no launch was attempted to be re-confirmed. It is deliberately
            # NOT in TerminationSource.resurrectable(): the row's restart history
            # is corrupt, and this function's contract is "raise once and stop", so
            # auto-retrying on the resurrect backoff would trade a loud one-time
            # integrity fault for a recurring background WARN that ops learns to
            # ignore. Reusing 'launch-confirm' here to save the enum value would do
            # exactly that. Ops resurrects by hand after looking at the row.
            # Capture open page names BEFORE the status flip — the
            # cascade_close_agent_pages trigger closes the page rows on
            # 'terminated', after which the open-page query matches nothing.
            # A 'restarting' row can hold open pages (restart keeps them
            # open across processes); the frontend popover needs the
            # PageClosed events to drop them.
            integrity_page_names = list_open_page_names(conn, agent_id)
            cur.execute(
                "UPDATE agents_meta SET status = 'terminated', "
                "termination_source = 'integrity', lease_expires_at = NULL WHERE id = %s",
                (agent_id,),
            )
            conn.commit()
            for page_name in integrity_page_names:
                publish_page_closed_sync(agent_id, page_name)
            raise RuntimeError(
                f"respawn_agent({agent_id}): status was 'restarting' but no 'restart' "
                f"inbound found — DB integrity violated; status forced to 'terminated', "
                f"caller may resurrect to retry."
            )
        original_source: str = row[0]
        original_content: str = row[1]
        # original_payload is the restart inbound's payload, trusted as
        # dict | None per its annotation. It is passed through verbatim into the
        # restart_completed INSERT below (drives the lifecycle marker diff). The
        # launch overlay is NOT derived from it — that comes from the column.
        original_payload: dict[str, object] | None = row[2]
        # Launch overlay is read from the authoritative column, NOT the restart
        # inbound payload. The payload still carries this-restart's diff and is
        # passed through to restart_completed so the lifecycle marker shows what
        # changed (see _render_restart_completed_marker).
        cur.execute(
            "SELECT config_overlay, birth_config FROM agents_meta WHERE id = %s", (agent_id,)
        )
        config_overlay: dict[str, object] | None
        birth_config: dict[str, object] | None
        config_overlay, birth_config = fetch_one(cur, "respawn: read per-agent config")
        # The restart_completed row is INSERTed first to pass through
        # source/content/payload; once the new process comes up and applies
        # the overlay, it INSERTs another restart_completed with
        # effective_config (full event trail, see agent/loop.py). This
        # function only guarantees that "the original restart's envelope"
        # lands in DB; snapshot writes on the new-process side are the claim
        # node's job.
        cur.execute(
            "INSERT INTO inbound_messages (agent_id, content, kind, source, payload) "
            "VALUES (%s, %s, 'restart_completed', %s, %s::jsonb)",
            (
                agent_id,
                original_content,
                original_source,
                json.dumps(original_payload) if original_payload else None,
            ),
        )
        # --- lifecycle event ---
        insert_event_log(
            event_type="restart_completed",
            agent_id=agent_id,
            source=original_source,
            payload={"config_overlay": config_overlay} if config_overlay else {},
        )
        conn.commit()
        publish_agent_updated_sync(conn, agent_id)
    agent_launch._kill_stale_session(agent_id)
    logger.info(
        "agent {agent_id} respawn phase 2: launching new process",
        event="respawn_phase2_launch",
        agent_id=agent_id,
    )
    agent_launch._launch_or_force_terminated(
        agent_id, config_overlay=config_overlay, birth_config=birth_config
    )
    logger.info(
        "agent {agent_id} restarted (origin source {origin})",
        event="agent_restarted",
        agent_id=agent_id,
        origin=original_source,
    )
    return True


def swap_in_agent(agent_id: int) -> bool:
    """Swap a hibernating agent's process back in: UPDATE 'hibernating' ->
    'allocated' (clearing pid/started_at) + launch a fresh process attached to
    the same agent_id, with NO lifecycle inbound.

    The clean-restart counterpart to `resurrect_agent` / `respawn_agent`: the same
    "UPDATE status + launch process" shape and the same launch mechanics
    (LangGraph auto-restores the checkpoint), but it INSERTs no inbound and writes
    no event_log. Hibernation is invisible to the agent, so the woken process
    finds only whatever inbound already triggered the wake (a heartbeat nudge, a
    chat, a task) in its first claim batch — exactly what a never-swapped idle
    agent would see. There is deliberately NO "you were hibernating" marker (unlike
    resurrect's resurrect inbound / respawn's restart_completed), which is the
    whole point: the agent cannot tell it was ever swapped out.

    Race-safe noop: the CAS `WHERE status='hibernating'` picks a single winner, so
    two concurrent swap-in attempts (the controller's poll racing its own next
    tick) cannot both launch. pid/started_at are cleared on the flip to
    'allocated' so a dead prior pid never lingers as ghost data (agent 44
    incident), matching resurrect/respawn.

    The caller MUST run this on the agent's home machine (`agents_meta.machine`) —
    launching the process on any other host trips the boot placement gate and
    crash-loops (agent 1513 incident). The hibernation controller satisfies this
    by only scanning `machine = local`.

    Returns:
        True: won the CAS, new process launched.
        False: lost the race / the row was not 'hibernating' — noop, does not raise.
    """
    with shared.db.connect() as conn, conn.cursor() as cur:
        # Race-safe gate: flip + commit so a concurrent swap-in sees 'allocated'
        # and loses. Clears pid/started_at (parked from the pre-hibernation
        # process), same as resurrect/respawn returning to 'allocated'.
        cur.execute(
            "UPDATE agents_meta SET status = %s, pid = NULL, started_at = NULL, "
            "lease_expires_at = NULL WHERE id = %s AND status = %s",
            (AgentStatus.ALLOCATED, agent_id, AgentStatus.HIBERNATING),
        )
        won_race = cur.rowcount == 1
        conn.commit()
        if not won_race:
            return False
        cur.execute(
            "SELECT config_overlay, birth_config FROM agents_meta WHERE id = %s", (agent_id,)
        )
        config_overlay: dict[str, object] | None
        birth_config: dict[str, object] | None
        config_overlay, birth_config = fetch_one(cur, "swap_in: read per-agent config")
        publish_agent_updated_sync(conn, agent_id)
    agent_launch._kill_stale_session(agent_id)
    agent_launch._launch_or_force_terminated(
        agent_id, config_overlay=config_overlay, birth_config=birth_config
    )
    logger.info(
        "agent {agent_id} swapped in from hibernation",
        event="agent_swapped_in",
        agent_id=agent_id,
    )
    return True


def revive_agent(agent_id: int, dead_pid: int) -> bool:
    """Revive a dead 'running'/'idling' row: CAS to 'allocated' + launch a fresh
    process, with NO lifecycle inbound.

    The boot-revive half of Task #689 G5. A machine reboot / power-off leaves
    every local agent row 'running'/'idling' behind a dead (or recycled) pid;
    the restarter reaper used to force those rows to 'terminated', and nothing
    ever brought the agents back (crash-resurrect needs a pending inbound, the
    heartbeat only targets idling/hibernating) -- the user-visible failure "the
    machine came back but the fleet stayed dead". This mirrors `swap_in_agent`
    exactly (same UPDATE-status + launch shape, same invisibility: the woken
    process finds its checkpoint and whatever inbound waited, no marker), but
    the CAS re-asserts the probed dead pid so a row whose process is actually
    alive (or already revived by a concurrent pass) is never double-launched.

    The caller MUST run this on the agent's home machine (`agents_meta.machine`)
    -- launching on any other host trips the boot placement gate and crash-loops
    (agent 1513 incident). The reaper satisfies this by only scanning
    `machine = local`.

    Crash-loop bound: if the revived process dies again at boot, the row lands
    'starting'/'allocated' and the existing reapers + launch-confirm force it to
    'terminated' -- at most one extra revive cycle per dead agent.

    Returns:
        True: won the CAS, new process launched.
        False: lost the race / the row is not 'running'/'idling' with that pid --
            noop, does not raise.
    """
    with shared.db.connect() as conn, conn.cursor() as cur:
        # Race-safe gate, pid-reasserted (ABA-closed like the reaper): flip +
        # commit so a concurrent revive/reap/launch sees 'allocated' and loses.
        # Clears pid/started_at -- the probed pid is a corpse (or recycled), it
        # must not linger as ghost data (agent 44 incident).
        cur.execute(
            "UPDATE agents_meta SET status = %s, pid = NULL, started_at = NULL "
            "WHERE id = %s AND status IN ('running', 'idling') AND pid = %s",
            (AgentStatus.ALLOCATED, agent_id, dead_pid),
        )
        won_race = cur.rowcount == 1
        conn.commit()
        if not won_race:
            return False
        cur.execute(
            "SELECT config_overlay, birth_config FROM agents_meta WHERE id = %s", (agent_id,)
        )
        config_overlay: dict[str, object] | None
        birth_config: dict[str, object] | None
        config_overlay, birth_config = fetch_one(cur, "revive: read per-agent config")
        publish_agent_updated_sync(conn, agent_id)
    agent_launch._kill_stale_session(agent_id)
    agent_launch._launch_or_force_terminated(
        agent_id, config_overlay=config_overlay, birth_config=birth_config
    )
    logger.info(
        "agent {agent_id} revived (dead pid {dead_pid})",
        event="agent_revived",
        agent_id=agent_id,
        dead_pid=dead_pid,
    )
    return True
