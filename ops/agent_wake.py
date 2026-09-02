"""Agent wake: an EXISTING agents_meta row back into a running process.

The other lifecycle half from `ops/agent_spawn.py` (which creates new rows);
both are reached through `ops/agents.py`. Two wake paths here, one shape: a CAS
on `agents_meta.status` into unclaimed 'idling' (clearing pid / started_at), a
lifecycle inbound, then a relaunch attached to the same `agent_id` —
LangGraph's checkpointer restores the message history, so the process resumes
rather than starts over. Resurrection commits allocation and OS authorization
before starting a detached session outside database locks. Early child admission
revalidates its exact allocation and deadline. `resurrect_agent` comes from
'terminated', `respawn_agent` from 'restarting'; the no-inbound wake path
(`revive_agent` from a dead pid) lives in `ops/agent_revive.py` (Task #1999
split, re-exported here). The *mechanics* of launching the child live in
`ops/agent_launch.py`, reached via module-qualified access
(`agent_launch._launch_or_force_terminated`).

Hosted mode (`AVA_RUNNER_MODE=hosted`): the CAS and the inbound rows are the
whole op — no process to launch. The hosted branches commit, publish the Redis
wake (the inbound INSERTs here are raw SQL and do not publish), and skip the
launch + pid-confirm machinery entirely.

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

import time
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

import psycopg
from psycopg import sql

from ops import agent_launch, runner_mode

# Re-exported so the existing importers (`ops.agents`, the restarter
# controllers, tests) keep their call sites unchanged after the Task #1999
# split: `revive_agent` now lives in `ops/agent_revive.py`.
from ops.agent_revive import revive_agent as revive_agent
from ops.pages import list_open_page_names
from ops.resurrection_retry import (
    ResurrectExitDeferredError,
    authorize_pending_retry,
    validate_pending_retry,
)
from shared.agents import (
    AgentNotFound,
    AgentStatus,
    MachinePaused,
    ResurrectAlreadyAlive,
    ResurrectBudgetExhausted,
    ResurrectError,
    TerminationSource,
)
from shared.audit_events import insert_event_log
from shared.config import field_alias, get_field, settings
from shared.daemon_health import health_port, probe_daemon
from shared.db import fetch_one, insert_restart_completed_inbound, publish_inbound_wake
from shared.db_transaction import write_transaction
from shared.lifecycle_termination_observe import observe_applied_termination
from shared.live_announce import publish_agent_updated_sync, publish_page_closed_sync
from shared.log import logger
from shared.machine import machine_name
from shared.resurrection_launch import authorize_launch, prepare_launch

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
    attempt_session: str | None = None
    command_id: int | None = None

class _ResurrectSessionStartError(RuntimeError):
    """Detached session creation failed before the resurrect commit."""

def _hosted_agent_host_healthy() -> bool:
    """Whether this hosted cluster's sole restart consumer is live and ours."""
    return probe_daemon(
        "agent_host",
        f"http://localhost:{health_port('agent_host')}/healthz",
        pidfile=settings.services.agent_host_pidfile,
    ).alive

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
        from shared.lifecycle_acceptance import FAILED_RESTART_FOR_CURRENT_TARGET

        assert trigger_inbound_kind is not None  # validated at public helper boundary  # noqa: S101
        cur.execute(
            sql.SQL(
                "UPDATE agents_meta SET status = %s, pid = NULL, started_at = NULL, "
                "termination_source = NULL, lease_expires_at = NULL "
                "WHERE id = %s AND status = %s "
                "AND NOT {} AND EXISTS ("
                "  SELECT 1 FROM inbound_messages m "
                "  WHERE m.id = %s AND m.agent_id = agents_meta.id "
                "    AND m.status = 'pending' AND m.kind = %s "
                "    AND m.created_at > agents_meta.status_changed_at "
                "    AND m.id > COALESCE(agents_meta.last_force_terminate_inbound_id, 0)"
                ") RETURNING status_changed_at"
            ).format(sql.SQL(FAILED_RESTART_FOR_CURRENT_TARGET)),
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

def _auto_resurrect_max_attempts() -> int:
    """Read the recovery budget after the gateway's runner-alias projection.

    Gateway processes omit runner-only aliases from their environment, while
    local hosted resurrection still runs this transaction in-process. The
    cluster `.env` remains the configuration authority in that profile.
    """
    from shared.runtime_config import read_env_aliases

    raw = read_env_aliases().get(field_alias("auto_resurrect_max_attempts"))
    if raw is not None:
        return int(raw)
    budget = get_field("auto_resurrect_max_attempts")
    if budget is not None:
        return int(budget)
    if settings.has_domain("daemon"):
        return settings.daemon.auto_resurrect_max_attempts
    raise RuntimeError("auto-resurrect budget has no configured daemon domain")

def _prepare_resurrect_attempt(
    agent_id: int,
    *,
    resurrected_by: str,
    prompt: str | None,
    trigger_inbound_id: int | None,
    trigger_inbound_kind: Literal["chat", "compact_request", "system_note"] | None,
    auto_claim: AutoResurrectClaim | None,
) -> _PreparedResurrect:
<<<<<<< HEAD
    """Transition, persist lifecycle rows, and create the session under one row lock."""
    from shared.envelope import reject_unnegotiated_caller

    reject_unnegotiated_caller(resurrected_by)
=======
    """Commit the allocation and its launch identity; never wait for OS work here."""
>>>>>>> 7f4039be5 (fix(lifecycle): commit resurrection launch budget before OS effects)
    with write_transaction() as conn, conn.cursor() as cur:
        _lock_active_home_machine(cur, agent_id)
        cur.execute("SELECT status FROM agents_meta WHERE id = %s FOR UPDATE", (agent_id,))
        row = cur.fetchone()
        if row is None:
            raise AgentNotFound(f"agent {agent_id} does not exist")
        current = AgentStatus(row[0])
        if current is not AgentStatus.TERMINATED:
            raise ResurrectAlreadyAlive(
                f"agent {agent_id} is in {current.value!r} state, not 'terminated'"
            )
        if resurrected_by == "system":
            cur.execute(
                "SELECT count(*) FROM inbound_messages "
                "WHERE agent_id = %s AND kind = 'resurrect' AND status = 'pending'",
                (agent_id,),
            )
            pending_resurrects = int(fetch_one(cur, "resurrect: count pending lifecycle rows")[0])
            if pending_resurrects >= _auto_resurrect_max_attempts():
                raise ResurrectBudgetExhausted(
                    f"agent {agent_id} has exhausted its auto-resurrect budget"
                )
        if trigger_inbound_id is not None:
            validate_pending_retry(conn, agent_id, trigger_inbound_id)
        if not observe_applied_termination(conn, agent_id, machine_name()):
            raise ResurrectExitDeferredError(
                "outstanding lifecycle target has not been observed ended"
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
            "VALUES (%s, '', 'resurrect', %s) RETURNING id",
            (agent_id, resurrected_by),
        )
        command_id = fetch_one(cur, "resurrect: persist launch identity")[0]
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
        if runner_mode.is_hosted():
            # Hosted: the dispatcher owns delivery — no process to fork. The
            # inbound INSERTs above are raw SQL (no wake inside), so publish one
            # explicitly; the dispatcher materializes a turn task and its claim
            # CAS is the hosted equivalent of the launch confirm. `resurrect_agent`
            # skips the pid-confirm machinery entirely in hosted mode.
            conn.commit()
            publish_agent_updated_sync(conn, agent_id)
            publish_inbound_wake(agent_id, "0")
            return _PreparedResurrect(
                allocation_epoch=allocation_epoch,
                config_overlay=config_overlay,
                birth_config=birth_config,
                event_target=_resurrect_event_target(resurrected_by),
            )
        prepare_launch(conn, agent_id, command_id, allocation_epoch, trigger_inbound_id)
        conn.commit()
        publish_agent_updated_sync(conn, agent_id)
    return _PreparedResurrect(
        allocation_epoch=allocation_epoch,
        config_overlay=config_overlay,
        birth_config=birth_config,
        event_target=_resurrect_event_target(resurrected_by),
        command_id=command_id,
    )

def _retry_resurrect_session(agent_id: int, prepared: _PreparedResurrect) -> str | None:
    """Commit one bounded authorization before creating its exact OS attempt."""
    if prepared.command_id is None:
        raise ResurrectError("resurrection allocation lacks a durable launch identity")
    with write_transaction() as conn, conn.cursor() as cur:
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
            return None
        attempt_number, remaining = authorize_launch(
            conn, agent_id, prepared.command_id, agent_launch._LAUNCH_MAX_RETRIES + 1
        )
    # A failure or crash here consumes the authorization. Admission independently
    # refuses a delayed attempt after the allocation changes or deadline passes.
    return agent_launch._launch_agent_process(
        agent_id,
        config_overlay=prepared.config_overlay,
        birth_config=prepared.birth_config,
        confirm=False,
        resurrect_attempt=(prepared.command_id, attempt_number, remaining),
    )

def _mark_resurrect_launch_failed(agent_id: int, prepared: _PreparedResurrect) -> None:
    """Terminate only the still-unclaimed allocation owned by this resurrect."""
    page_names: list[str] = []
    changed = False
    with write_transaction() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT status, status_changed_at, pid FROM agents_meta WHERE id = %s FOR UPDATE",
            (agent_id,),
        )
        row = cur.fetchone()
        if row == (AgentStatus.IDLING.value, prepared.allocation_epoch, None):
            page_names = list_open_page_names(conn, agent_id)
            agent_launch._require_released_agent_session(agent_id)
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
    attempt_session = prepared.attempt_session
    while True:
        try:
            if attempt_session is None:
                attempt_session = _retry_resurrect_session(agent_id, prepared)
                if attempt_session is None:
                    return False
            agent_launch._wait_for_agent_claim(agent_id, attempt_session)
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
                attempt_session = _retry_resurrect_session(agent_id, prepared)
                if attempt_session is None:
                    return False
            except RuntimeError:
                attempt_session = None
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
    content=prompt, source=resurrected_by). Allocation and a bounded launch
    authorization commit before the detached session is created outside locks.
    Early child admission validates that exact allocation and original deadline.
    When the agent wakes and SELECTs the batch, both rows are visible, with
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
        ResurrectBudgetExhausted: a system-initiated resurrect reached the
            configured limit of pending lifecycle rows. Manual resurrects are
            exempt so an operator can recover the agent after correcting its
            underlying failure.
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
        if trigger_inbound_id is not None:
            if trigger_inbound_kind is None:
                raise ValueError("pending resurrection trigger kind is required")
            authorize_pending_retry(
                agent_id,
                trigger_inbound_id,
                trigger_inbound_kind,
                agent_launch._LAUNCH_MAX_RETRIES + 1,
            )
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
        except (_ResurrectSessionStartError, ResurrectExitDeferredError):
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
    # Hosted has no pid to wait for: the transition + inbound are committed and
    # the wake is published (both in _prepare_resurrect_attempt), and the
    # dispatcher's turn task is the confirmation. The process-mode confirm polls
    # `agents_meta.pid`, which hosted never writes — running it here would time
    # out and force-terminate a row that is working exactly as designed.
    confirmed = (
        True
        if runner_mode.is_hosted()
        else _confirm_resurrect_with_retries(agent_id, prepared, first_attempt=first_attempt)
    )
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
    from ops.lifecycle_recovery import recover_lifecycle_command

    recovered = recover_lifecycle_command(agent_id)
    if recovered is not None:
        return recovered
    hosted = runner_mode.is_hosted()
    if hosted and not _hosted_agent_host_healthy():
        logger.error(
            "agent {agent_id} restart handoff failed: hosted agent-host is unhealthy; "
            "leaving row restarting for retry",
            event="restart_handoff_host_unhealthy",
            agent_id=agent_id,
        )
        return False

    with write_transaction() as conn, conn.cursor() as cur:
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

        # Phase 2: trace the original restart inbound into restart_completed.
        # Status 'restarting' implies an earlier 'restart' inbound triggered
        # the claim to switch status to 'restarting' (the claim_node case
        # "restart" branch only marks RESTARTING after receiving a restart
        # inbound). row is None = DB integrity violation (ops manually
        # UPDATEd bypassing the inbound path / migration bug); raise to make
        # it visible to ops. At this point status is unclaimed 'idling', restarter
        # will not retrigger (raise once and stop).
        #
        restart_trace = insert_restart_completed_inbound(cur, agent_id)
        if restart_trace is None:
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
        original_source = restart_trace[0]
        # The launch overlay is read from the authoritative column, NOT the restart
        # inbound payload. The payload still carries this-restart's diff and is
        # passed through to restart_completed so the lifecycle marker shows what
        # changed (see _render_restart_completed_marker).
        cur.execute(
            "SELECT config_overlay, birth_config FROM agents_meta WHERE id = %s", (agent_id,)
        )
        config_overlay: dict[str, object] | None
        birth_config: dict[str, object] | None
        config_overlay, birth_config = fetch_one(cur, "respawn: read per-agent config")
        conn.commit()
        publish_agent_updated_sync(conn, agent_id)
    if hosted:
        # Hosted restart (PR-side change) never flips a row to 'restarting', so
        # this branch is defensive — but if a row does land here, the hosted
        # answer is the same as everywhere else: the dispatcher owns delivery.
        # No process, no stale-session kill; the restart_completed inbound above
        # is raw SQL, so publish the wake explicitly.
        publish_inbound_wake(agent_id, "0")
        logger.info(
            "agent {agent_id} restarted in hosted mode (origin source {origin}) — "
            "wake published, no process launched",
            event="agent_restarted",
            agent_id=agent_id,
            origin=original_source,
        )
        return True
    agent_launch._require_released_agent_session(agent_id)
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
