"""Durable gates consulted before an automatic resurrection.

Split out of `ops/ops_lifecycle.py` when the closure guard pushed that module
at its line budget. Each function answers one question about agents_meta /
inbound_messages state — may an automatic wake proceed? — and each is its own
durable policy: wake suppression (repeated resurrect failures), the recovery
breaker (consecutive permanent provider rejections), the system-notice source
(notifications never resurrect), and the closure marker (`terminate --final`:
never auto-resurrect). Read failures propagate: a failed read must never
degrade into a wake the policy forbids.
"""

import shared.db
from shared.agents import AgentNotFound
from shared.lifecycle_acceptance import is_closed_agent, is_system_notice_source


def wake_suppression_active(agent_id: int) -> bool:
    with shared.db.connect() as conn:
        row = conn.execute(
            "SELECT wake_suppressed_until >= now() FROM agents_meta WHERE id=%s",
            (agent_id,),
        ).fetchone()
    if row is None:
        raise AgentNotFound(f"agent {agent_id} does not exist")
    return row[0] is True


def recovery_halted(agent_id: int) -> bool:
    """Whether the recovery circuit breaker is tripped for `agent_id`.

    The durable gate is `permanent_reject_streak` (>= the halt threshold after
    consecutive permanent provider rejections) — NOT the wake-suppression
    window, which a claim clears by design; only the streak can carry an
    until-human halt (task #3617)."""
    from shared.recovery_breaker import HALT_AFTER_CONSECUTIVE_PERMANENT_REJECTS

    with shared.db.connect() as conn:
        row = conn.execute(
            "SELECT permanent_reject_streak >= %s FROM agents_meta WHERE id=%s",
            (HALT_AFTER_CONSECUTIVE_PERMANENT_REJECTS, agent_id),
        ).fetchone()
    if row is None:
        raise AgentNotFound(f"agent {agent_id} does not exist")
    return row[0] is True


def system_notice_source_of_trigger(agent_id: int, trigger_inbound_id: int) -> str | None:
    """The trigger's `source` when it is a system-family chat notice — those
    never resurrect their owner (user ruling 2026-08-27; task #3687) — else
    None.

    The delivery watchdog's hosted-turn recovery chat is NOT a notice: its
    payload carries the `hosted_turn_recovery` marker, and the shared
    predicate (fail-closed on any non-boolean marker value) returns False, so
    the recovery reaches dispatch (task #3687 review, Ava #3242).

    No row, a non-chat kind, or any other source returns None so the caller
    proceeds on the normal resurrect path; the home runner's final CAS still
    adjudicates stale work. A DB read failure propagates: a failed read must
    not be silently swallowed into a "skip" (the suppression / breaker checks
    above fail loudly the same way).
    """
    with shared.db.connect() as conn:
        row = conn.execute(
            "SELECT kind, source, payload FROM inbound_messages WHERE id=%s AND agent_id=%s",
            (trigger_inbound_id, agent_id),
        ).fetchone()
    if row is None:
        return None
    kind, source, payload = row
    if kind == "chat" and is_system_notice_source(source, payload):
        return str(source)
    return None


def recovery_halt_reason(agent_id: int) -> str | None:
    """Why automatic recovery is halted for `agent_id`, else None — the
    reason-resolution sibling of `recovery_halted` for the stalled-harvest
    requester (task #3618).

    The durable gate is the recovery breaker's streak
    (`RECOVERY_BREAKER_CLEAR` inverts it: consecutive permanent provider
    rejections with no successful turn between them), which even a claim
    cannot clear; it always reports `permanent_provider_reject`. An active
    wake-suppression window without a tripped breaker reports its
    operator-readable reason (or the `wake_suppressed` fallback)."""
    from shared.recovery_breaker import (
        HALT_AFTER_CONSECUTIVE_PERMANENT_REJECTS,
        SUPPRESS_REASON_PERMANENT_REJECT,
    )

    with shared.db.connect() as conn:
        row = conn.execute(
            "SELECT permanent_reject_streak >= %s, wake_suppress_reason, "
            "(wake_suppressed_until IS NOT NULL AND wake_suppressed_until >= now()) "
            "FROM agents_meta WHERE id=%s",
            (HALT_AFTER_CONSECUTIVE_PERMANENT_REJECTS, agent_id),
        ).fetchone()
    if row is None:
        return None
    halted, suppress_reason, suppress_active = row
    if halted:
        return SUPPRESS_REASON_PERMANENT_REJECT
    if suppress_active:
        return suppress_reason or "wake_suppressed"
    return None


def clear_wake_suppression(agent_id: int) -> None:
    with shared.db.connect() as conn:
        conn.execute(
            "UPDATE agents_meta SET wake_suppressed_until=NULL, wake_suppress_reason=NULL "
            "WHERE id=%s AND wake_suppressed_until IS NOT NULL",
            (agent_id,),
        )


def closed_agent(agent_id: int) -> bool:
    """Whether the user closed `agent_id` — never auto-resurrect (`closed_at` set).

    The closure marker outranks every automatic channel, including the
    system-notice carve-out: only an explicit manual resurrect reopens (and
    clears) the agent. Read shape mirrors `wake_suppression_active` /
    `recovery_halted` — a DB read failure propagates, never a silent skip.
    """
    with shared.db.connect() as conn:
        row = conn.execute(
            "SELECT closed_at FROM agents_meta WHERE id=%s",
            (agent_id,),
        ).fetchone()
    if row is None:
        raise AgentNotFound(f"agent {agent_id} does not exist")
    return is_closed_agent(row[0])
