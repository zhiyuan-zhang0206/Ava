"""Agent wake: an EXISTING agents_meta row back into a running process.

The other lifecycle half from `ops/agent_spawn.py` (which creates new rows);
both are reached through `ops/agents.py`. Three wake paths, one shape: a CAS on
`agents_meta.status` into unclaimed 'idling' (clearing pid / started_at), an optional
lifecycle inbound, then a relaunch attached to the same `agent_id` — LangGraph's
checkpointer restores the message history, so the process resumes rather than
starts over. Resurrection creates the detached session while its machine and
agent row locks are held; the child cannot claim the row or process the inbound
until that transaction commits. `resurrect_agent` comes from
'terminated', `respawn_agent` from 'restarting', `swap_in_agent` from
'hibernating'; only the first two write a lifecycle inbound, because
hibernation is deliberately invisible to the agent. The *mechanics* of launching
the child live in `ops/agent_launch.py`, reached via module-qualified access
(`agent_launch._launch_or_force_terminated`).

- **resurrect_agent(...)** — a 'terminated' row -> unclaimed 'idling' + launch.
  INSERTs a kind='resurrect' inbound so the LLM sees why it resumed; an
  optional `prompt` adds a chat in the same transaction. The child cannot claim
  or process either inbound
  before the transaction commits, eliminating the race "prompt has not arrived
  yet but agent has already started running". Pending-work auto-resurrect also
  carries the exact inbound id and expected kind (`chat` or `compact_request`):
  the final UPDATE accepts it only while that row is pending, newer than the
  current termination, and above the latest force-terminate inbound fence. A
  later kill therefore wins without a marker or launch. Crash and wedged
  recovery instead carry an exact controller claim for the current death;
  explicit manual resurrection has neither automatic-work guard.
- **respawn_agent(agent_id)** — 'restarting' -> unclaimed 'idling' + launch process,
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
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

import psycopg

import shared.db
from ops import agent_launch
from ops.pages import list_open_page_names
from shared.agents import (
    AgentNotFound,
    AgentStatus,
    MachinePaused,
    ResurrectAlreadyAlive,
    ResurrectError,
    TerminationSource,
)
from shared.audit_events import insert_event_log
from shared.db import fetch_one
from shared.live_announce import publish_agent_updated_sync, publish_page_closed_sync
from shared.log import logger


class ResurrectTriggerStaleError(ResurrectError):
    """An auto-resurrect trigger no longer qualifies for the current death.

    Internal and non-wire: the lifecycle op turns it into an idempotent no-op.
    Unlike `ResurrectAlreadyAlive`, the agent can still be terminated.
    """


class ResurrectClaimStaleError(ResurrectError):
    """A controller's auto-resurrect claim no longer owns this death."""


@dataclass(frozen=True)
class AutoResurrectClaim:
    """Persistent token carried from a controller claim to the final CAS."""

    agent_id: int
    termination_source: TerminationSource
    termination_epoch: datetime
    claim_kind: Literal["crash", "wedged"]
    claimed_at: datetime


@dataclass(frozen=True)
class _PreparedResurrect:
    allocation_epoch: datetime
    config_overlay: dict[str, object] | None
    birth_config: dict[str, object] | None
    event_target: int | None


class _ResurrectSessionStartError(RuntimeError):
    """Detached session creation failed before the resurrect commit."""


def _lock_active_home_machine(cur: psycopg.Cursor, agent_id: int) -> None:
    """Lock the home-machine admission row before locking the agent row.

    Machine pause takes the inverse side of this same lock first, commits the
    latch, then sweeps agents. A resurrection that wins the share lock may
    finish and is swept; one that loses observes ``paused_at`` and cannot
    transition. The global lock order is therefore machine -> agent.
    """
    cur.execute("SELECT machine FROM agents_meta WHERE id = %s", (agent_id,))
    agent_row = cur.fetchone()
    if agent_row is None:
        raise AgentNotFound(f"agent {agent_id} does not exist")
    home_machine = agent_row[0]
    cur.execute("SELECT paused_at FROM machines WHERE name = %s FOR SHARE", (home_machine,))
    machine_row = cur.fetchone()
    if machine_row is not None and machine_row[0] is not None:
        raise MachinePaused(
            f"agent {agent_id} home machine {home_machine!r} is paused; "
            "resume it before resurrecting"
        )


def _transition_terminated_to_unclaimed_idling(
    cur: psycopg.Cursor,
    agent_id: int,
    *,
    trigger_inbound_id: int | None,
    trigger_inbound_kind: Literal["chat", "compact_request", "system_note"] | None,
    auto_claim: AutoResurrectClaim | None,
) -> datetime:
    """Run the one final resurrection CAS with a fully static SQL shape."""
    base_params = (AgentStatus.IDLING, agent_id, AgentStatus.TERMINATED)
    if trigger_inbound_id is not None:
        assert trigger_inbound_kind is not None  # validated at public helper boundary  # noqa: S101
        cur.execute(
            "UPDATE agents_meta SET status = %s, pid = NULL, started_at = NULL, "
            "termination_source = NULL, lease_expires_at = NULL "
            "WHERE id = %s AND status = %s "
            "AND EXISTS ("
            "  SELECT 1 FROM inbound_messages m "
            "  WHERE m.id = %s AND m.agent_id = agents_meta.id "
            "    AND m.status = 'pending' AND m.kind = %s "
            "    AND m.created_at > agents_meta.status_changed_at "
            "    AND m.id > COALESCE(agents_meta.last_force_terminate_inbound_id, 0)"
            ") RETURNING status_changed_at",
            (*base_params, trigger_inbound_id, trigger_inbound_kind),
        )
    elif auto_claim is not None and auto_claim.claim_kind == "crash":
        cur.execute(
            "UPDATE agents_meta SET status = %s, pid = NULL, started_at = NULL, "
            "termination_source = NULL, lease_expires_at = NULL "
            "WHERE id = %s AND status = %s "
            "AND termination_source = %s AND status_changed_at = %s "
            "AND last_resurrect_at = %s RETURNING status_changed_at",
            (
                *base_params,
                auto_claim.termination_source.value,
                auto_claim.termination_epoch,
                auto_claim.claimed_at,
            ),
        )
    elif auto_claim is not None and auto_claim.claim_kind == "wedged":
        cur.execute(
            "UPDATE agents_meta SET status = %s, pid = NULL, started_at = NULL, "
            "termination_source = NULL, lease_expires_at = NULL "
            "WHERE id = %s AND status = %s "
            "AND termination_source = %s AND status_changed_at = %s "
            "AND last_wedged_check_at = %s RETURNING status_changed_at",
            (
                *base_params,
                auto_claim.termination_source.value,
                auto_claim.termination_epoch,
                auto_claim.claimed_at,
            ),
        )
    else:
        cur.execute(
            "UPDATE agents_meta SET status = %s, pid = NULL, started_at = NULL, "
            "termination_source = NULL, lease_expires_at = NULL "
            "WHERE id = %s AND status = %s RETURNING status_changed_at",
            base_params,
        )
    transition_row = cur.fetchone()
    if transition_row is not None:
        return transition_row[0]
    cur.execute(
        "SELECT home.paused_at IS NOT NULL "
        "FROM agents_meta a JOIN machines home ON home.name = a.machine "
        "WHERE a.id = %s",
        (agent_id,),
    )
    paused_row = cur.fetchone()
    if paused_row is not None and paused_row[0] is True:
        raise MachinePaused(
            f"agent {agent_id} home machine is paused; resume it before resurrecting"
        )
    if trigger_inbound_id is not None:
        raise ResurrectTriggerStaleError(
            f"agent {agent_id} trigger work no longer qualifies for its current "
            "termination; UPDATE affected 0 rows"
        )
    if auto_claim is not None:
        raise ResurrectClaimStaleError(
            f"agent {agent_id} auto-resurrect claim no longer owns the current "
            "termination; UPDATE affected 0 rows"
        )
    raise ResurrectAlreadyAlive(
        f"agent {agent_id} was concurrently modified after SELECT; UPDATE affected 0 rows"
    )


def _resurrect_event_target(resurrected_by: str) -> int | None:
    if not resurrected_by.startswith("agent:"):
        return None
    try:
        return int(resurrected_by.removeprefix("agent:"))
    except ValueError:
        return None


def _prepare_resurrect_attempt(
    agent_id: int,
    *,
    resurrected_by: str,
    prompt: str | None,
    trigger_inbound_id: int | None,
    trigger_inbound_kind: Literal["chat", "compact_request", "system_note"] | None,
    auto_claim: AutoResurrectClaim | None,
) -> _PreparedResurrect:
    """Transition, persist lifecycle rows, and create the session under one row lock."""
    with shared.db.connect() as conn, conn.cursor() as cur:
        _lock_active_home_machine(cur, agent_id)
        cur.execute("SELECT status FROM agents_meta WHERE id = %s", (agent_id,))
        row = cur.fetchone()
        if row is None:
            raise AgentNotFound(f"agent {agent_id} does not exist")
        current = AgentStatus(row[0])
        if current is not AgentStatus.TERMINATED:
            raise ResurrectAlreadyAlive(
                f"agent {agent_id} is in {current.value!r} state, not 'terminated'"
            )
        allocation_epoch = _transition_terminated_to_unclaimed_idling(
            cur,
            agent_id,
            trigger_inbound_id=trigger_inbound_id,
            trigger_inbound_kind=trigger_inbound_kind,
            auto_claim=auto_claim,
        )
        cur.execute(
            "INSERT INTO inbound_messages (agent_id, content, kind, source) "
            "VALUES (%s, '', 'resurrect', %s)",
            (agent_id, resurrected_by),
        )
        if prompt is not None:
            cur.execute(
                "INSERT INTO inbound_messages (agent_id, content, kind, source) "
                "VALUES (%s, %s, 'chat', %s)",
                (agent_id, prompt, resurrected_by),
            )
        cur.execute(
            "SELECT config_overlay, birth_config FROM agents_meta WHERE id = %s", (agent_id,)
        )
        config_overlay: dict[str, object] | None
        birth_config: dict[str, object] | None
        config_overlay, birth_config = fetch_one(cur, "resurrect: read per-agent config")
        try:
            agent_launch._launch_agent_process(
                agent_id,
                config_overlay=config_overlay,
                birth_config=birth_config,
                confirm=False,
            )
        except RuntimeError as exc:
            raise _ResurrectSessionStartError(str(exc)) from exc
        conn.commit()
        publish_agent_updated_sync(conn, agent_id)
    return _PreparedResurrect(
        allocation_epoch=allocation_epoch,
        config_overlay=config_overlay,
        birth_config=birth_config,
        event_target=_resurrect_event_target(resurrected_by),
    )


def _retry_resurrect_session(agent_id: int, prepared: _PreparedResurrect) -> bool:
    """Create another session only while the same active allocation still owns the row."""
    with shared.db.connect() as conn, conn.cursor() as cur:
        _lock_active_home_machine(cur, agent_id)
        cur.execute(
            "SELECT status, status_changed_at, pid FROM agents_meta WHERE id = %s FOR UPDATE",
            (agent_id,),
        )
        row = cur.fetchone()
        if (
            row is None
            or row[0] != AgentStatus.IDLING.value
            or row[1] != prepared.allocation_epoch
            or row[2] is not None
        ):
            return False
        agent_launch._launch_agent_process(
            agent_id,
            config_overlay=prepared.config_overlay,
            birth_config=prepared.birth_config,
            confirm=False,
        )
    return True


def _mark_resurrect_launch_failed(agent_id: int, prepared: _PreparedResurrect) -> None:
    """Terminate only the still-unclaimed allocation owned by this resurrect."""
    page_names: list[str] = []
    changed = False
    with shared.db.connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT status, status_changed_at, pid FROM agents_meta WHERE id = %s FOR UPDATE",
            (agent_id,),
        )
        row = cur.fetchone()
        if row == (AgentStatus.IDLING.value, prepared.allocation_epoch, None):
            page_names = list_open_page_names(conn, agent_id)
            agent_launch._kill_stale_session(agent_id)
            cur.execute(
                "UPDATE agents_meta SET status = 'terminated', "
                "termination_source = 'launch-confirm' "
                "WHERE id = %s AND status = 'idling' AND pid IS NULL AND status_changed_at = %s",
                (agent_id, prepared.allocation_epoch),
            )
            changed = cur.rowcount == 1
        conn.commit()
        if changed:
            publish_agent_updated_sync(conn, agent_id)
    if changed:
        for page_name in page_names:
            publish_page_closed_sync(agent_id, page_name)


def _confirm_resurrect_with_retries(
    agent_id: int, prepared: _PreparedResurrect, *, first_attempt: int
) -> bool:
    """Confirm one incarnation, retrying sessions without reopening an old death."""
    attempt = first_attempt
    while True:
        try:
            agent_launch._wait_for_agent_claim(agent_id)
            return True
        except RuntimeError as exc:
            if attempt >= agent_launch._LAUNCH_MAX_RETRIES:
                _mark_resurrect_launch_failed(agent_id, prepared)
                raise
            backoff = agent_launch._LAUNCH_RETRY_BASE_BACKOFF_SEC * 2**attempt
            logger.warning(
                "agent {id} resurrect launch attempt {n}/{total} failed ({exc}); "
                "retrying the same allocation in {backoff:.0f}s",
                id=agent_id,
                n=attempt + 1,
                total=agent_launch._LAUNCH_MAX_RETRIES + 1,
                exc=repr(exc),
                backoff=backoff,
            )
            time.sleep(backoff)
            attempt += 1
            try:
                if not _retry_resurrect_session(agent_id, prepared):
                    return False
            except RuntimeError:
                if attempt >= agent_launch._LAUNCH_MAX_RETRIES:
                    _mark_resurrect_launch_failed(agent_id, prepared)
                    raise
                continue


def resurrect_agent(
    agent_id: int,
    *,
    resurrected_by: str,
    prompt: str | None = None,
    trigger_inbound_id: int | None = None,
    trigger_inbound_kind: Literal["chat", "compact_request", "system_note"] | None = None,
    auto_claim: AutoResurrectClaim | None = None,
) -> int:
    """Resurrect an already-terminated agent: UPDATE 'terminated' -> unclaimed 'idling'
    + INSERT one kind='resurrect' inbound (source=resurrected_by) +
    (optionally) one 'chat' prompt inbound + launch process.

    Agent, checkpoints, and messages stay in DB; LangGraph restores the last
    state. The resurrect inbound acts as a lifecycle signal whose claim appends
    a marker
    "[system ts] You have been resurrected by {resurrected_by}" to messages,
    so the LLM sees that it was resurrected. It is always present, even without
    a prompt.

    `prompt` is optional. When given, INSERT one 'chat' inbound in **the same
    transaction** as the lifecycle 'resurrect' inbound (kind='chat',
    content=prompt, source=resurrected_by). The detached session is created
    before commit while the machine and agent locks are held, but its child
    blocks on the agent row and cannot claim or process either inbound until
    commit. When the agent wakes and SELECTs the batch, both rows are visible, with
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
        trigger_inbound_id: optional pending-work auto-resurrect guard. When
            set with `trigger_inbound_kind`, the terminated -> idling
            transition wins only if this exact chat or compact request is still
            pending, was created after the current death, and has an id above
            the latest explicit-kill fence. Explicit manual and controller
            recovery callers leave it None.
        trigger_inbound_kind: expected kind for `trigger_inbound_id`; either
            `chat` or `compact_request`.
        auto_claim: optional controller claim token. The final transition
            additionally requires the same involuntary termination source and
            persistent claim timestamp, so a later explicit force cannot be
            reversed by a crash/wedged task already in flight.

    Returns:
        agent_id (symmetric with create_agent_row).

    Raises:
        AgentNotFound: agent_id does not exist.
        ResurrectAlreadyAlive: agent is in a non-'terminated' state
            (running / idling / restarting) — any state
            that means "still alive or not fully dead" should not spawn a
            second process.
        ResurrectTriggerStaleError: `trigger_inbound_id` no longer names the
            expected pending post-termination work for this agent. The agent can still be
            terminated; the internal auto-resurrect caller treats this as a
            no-op.
        ResurrectClaimStaleError: `auto_claim` no longer owns the terminated
            row. The controller treats this as a benign stale no-op.
    """
    if (trigger_inbound_id is None) != (trigger_inbound_kind is None):
        raise ValueError("trigger inbound id and kind must be provided together")
    if trigger_inbound_id is not None and auto_claim is not None:
        raise ValueError("chat trigger and controller claim guards are mutually exclusive")
    if auto_claim is not None and auto_claim.agent_id != agent_id:
        raise ValueError(
            f"auto-resurrect claim belongs to agent {auto_claim.agent_id}, not {agent_id}"
        )
    prepared: _PreparedResurrect | None = None
    first_attempt = 0
    for first_attempt in range(agent_launch._LAUNCH_MAX_RETRIES + 1):
        try:
            prepared = _prepare_resurrect_attempt(
                agent_id,
                resurrected_by=resurrected_by,
                prompt=prompt,
                trigger_inbound_id=trigger_inbound_id,
                trigger_inbound_kind=trigger_inbound_kind,
                auto_claim=auto_claim,
            )
            break
        except _ResurrectSessionStartError:
            if first_attempt >= agent_launch._LAUNCH_MAX_RETRIES:
                raise
            backoff = agent_launch._LAUNCH_RETRY_BASE_BACKOFF_SEC * 2**first_attempt
            time.sleep(backoff)
    if prepared is None:
        raise AssertionError("resurrect preparation loop exhausted without result")
    insert_event_log(
        event_type="resurrect",
        agent_id=agent_id,
        source=resurrected_by,
        target_agent_id=prepared.event_target,
        payload={"prompt": prompt} if prompt else {},
    )
    confirmed = _confirm_resurrect_with_retries(agent_id, prepared, first_attempt=first_attempt)
    logger.info(
        "agent {agent_id} resurrected by {resurrected_by} (confirmed={confirmed})",
        event="agent_resurrected",
        agent_id=agent_id,
        resurrected_by=resurrected_by,
        confirmed=confirmed,
    )
    return agent_id


def respawn_agent(agent_id: int) -> bool:
    """Called by the restarter daemon: UPDATE 'restarting' -> unclaimed 'idling' +
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
    status='idling' commit takes effect, restarter no longer polls this
    agent, and a raise in subsequent steps will not trigger infinite retry
    (on raise, agent is stuck unclaimed at 'idling', matching launch-failure
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
            (AgentStatus.IDLING, agent_id, AgentStatus.RESTARTING),
        )
        won_race = cur.rowcount == 1
        conn.commit()
        if not won_race:
            return False
        logger.info(
            "agent {agent_id} respawn won race, committing phase 1 (restarting -> idling)",
            event="respawn_phase1",
            agent_id=agent_id,
        )

        # Phase 2: find the original 'restart' inbound's source + content + payload, INSERT 'restart_completed'.
        # Status 'restarting' implies an earlier 'restart' inbound triggered
        # the claim to switch status to 'restarting' (the claim_node case
        # "restart" branch only marks RESTARTING after receiving a restart
        # inbound). row is None = DB integrity violation (ops manually
        # UPDATEd bypassing the inbound path / migration bug); raise to make
        # it visible to ops. At this point status is unclaimed 'idling', restarter
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
            # Capture cascade-closable show() page names BEFORE the status
            # flip. Daemon-supervised serve() pages stay open; the frontend
            # only needs PageClosed events for pages the cascade closes.
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
    unclaimed 'idling' (clearing pid/started_at) + launch a fresh process attached to
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
    unclaimed 'idling' so a dead prior pid never lingers as ghost data (agent 44
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
        # Race-safe gate: flip + commit so a concurrent swap-in sees unclaimed 'idling'
        # and loses. Clears pid/started_at (parked from the pre-hibernation
        # process), same as resurrect/respawn returning to unclaimed 'idling'.
        cur.execute(
            "UPDATE agents_meta SET status = %s, pid = NULL, started_at = NULL, "
            "lease_expires_at = NULL WHERE id = %s AND status = %s",
            (AgentStatus.IDLING, agent_id, AgentStatus.HIBERNATING),
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
    """Revive a dead 'running'/'idling' row: CAS to unclaimed 'idling' + launch a fresh
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
    as a boot-phase death and the reapers + launch-confirm force it to
    'terminated' -- at most one extra revive cycle per dead agent.

    Returns:
        True: won the CAS, new process launched.
        False: lost the race / the row is not 'running'/'idling' with that pid --
            noop, does not raise.
    """
    with shared.db.connect() as conn, conn.cursor() as cur:
        # Race-safe gate, pid-reasserted (ABA-closed like the reaper): flip +
        # commit so a concurrent revive/reap/launch sees unclaimed 'idling' and loses.
        # Clears pid/started_at -- the probed pid is a corpse (or recycled), it
        # must not linger as ghost data (agent 44 incident).
        cur.execute(
            "UPDATE agents_meta SET status = %s, pid = NULL, started_at = NULL, "
            "lease_expires_at = NULL "
            "WHERE id = %s AND status IN ('running', 'idling') AND pid = %s",
            (AgentStatus.IDLING, agent_id, dead_pid),
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
