"""One acceptance writer for live runtimes and verified cold controllers.

Callers establish their distinct authority before calling: a current owner with
a fresh lease, or a local controller that positively proved no admitted owner.
Both use this exact transaction and target fence. Chat never participates.

Acceptance also owns the incarnation-epoch boundary: a lifecycle command whose
intent predates the latest resurrection is settled as superseded instead of
being adopted, so a resurrect can never replay an older terminate (issue #2158).
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, LiteralString
from uuid import UUID

import psycopg
from psycopg.pq import TransactionStatus
from psycopg.types.json import Jsonb

from shared.runtime_incarnation import RuntimeIncarnation

# Correlated against the unaliased agents_meta row. Selection is an optimization;
# the same predicate also fences the final automatic resurrection UPDATE.
FAILED_RESTART_FOR_CURRENT_TARGET: LiteralString = (
    "EXISTS(SELECT 1 FROM inbound_messages failed WHERE failed.agent_id=agents_meta.id "
    "AND failed.target_generation=agents_meta.runtime_generation "
    "AND failed.target_owner=agents_meta.runtime_owner AND failed.kind='restart' "
    "AND failed.status='done' AND failed.applied_at IS NOT NULL AND failed.observed_at IS NULL "
    "AND failed.payload->'lifecycle_result'->>'outcome'='failed' "
    "AND failed.payload->'lifecycle_result'->>'reason'='restart_deadline_expired')"
)

# A row the SYSTEM harvested rather than one an operator ended: the corpse
# reaper terminated a crash-marked hosted corpse (`termination_source` =
# 'reaper' AND `last_turn_fatal_at` IS NOT NULL). Both fields survive the whole
# terminated window — the resurrect transition clears them together — so the
# pair names exactly "system-reaped crash corpse". The trigger guard waives the
# created_at>status_changed_at fence for these rows: work that predates a
# system reap is still work to resume, unlike work predating a user's explicit
# death (task #3617, design #3610 section 6).
SYSTEM_REAPED_CRASH_ROW: LiteralString = (
    "(agents_meta.termination_source = 'reaper' AND agents_meta.last_turn_fatal_at IS NOT NULL)"
)

# A system-family chat is a platform notification, and notifications never
# resurrect a terminated owner (user ruling 2026-08-27; task #3687). `system`
# and its `system:<subtype>` variants are the data-plane shape of framework
# notices (watcher reclamation notices, operator alerts): they stay queued and
# deliver on the owner's next resurrect through any other channel, or are
# dead-lettered past the stale threshold. Machine *wakeups* (watcher: / shell:
# / schedule:) are deliberately NOT in this family — a crash-reaped owner's
# watcher wake is a revival channel, and the reap pass clears a permanently
# terminated owner's watchers. The delivery watchdog's selection and the
# resurrect endpoint share this predicate; they must be updated together (the
# parity test pins the SQL fragment and the Python twin to each other).
#
# Carve-out: the delivery watchdog's own hosted-turn recovery chat
# (`HOSTED_TURN_RECOVERY_MARKER` below) is a system-source message that MUST
# reach both resurrection channels — it is this machinery's durable retry for
# a wedged hosted turn, not a notification. Any future recovery-class system
# message must set the same marker or it defaults to "notice, never
# resurrects". Marker strictness is fail-closed: only the exact JSON boolean
# `true` exempts — a missing key, JSON null, or any other value (even the
# string "true") stays a notice. The fragment embeds the marker key literally
# (the parity test catches drift), `->>` is rejected on purpose (it collapses
# boolean true and the string "true" into the same text), and
# `COALESCE(..., false)` keeps the predicate two-valued — never `NOT(NULL)`.
# The Python twin mirrors both rules exactly (`is not True`, never truthy —
# `1 == True` in Python).
#
# Assumes the `inbound_messages` alias `m`, like the fragments above assume
# `agents_meta`. `starts_with` (not `LIKE 'system:%'`) on purpose: a literal
# `%` in a parameterized psycopg query would need `%%` escaping and is one
# edit away from silently matching the wrong rows; `starts_with` mirrors the
# Python twin's `startswith` exactly.
HOSTED_TURN_RECOVERY_MARKER = "hosted_turn_recovery"

SYSTEM_NOTICE_SOURCE: LiteralString = (
    "((m.source = 'system' OR starts_with(m.source, 'system:')) "
    "AND NOT COALESCE((m.payload -> 'hosted_turn_recovery') = 'true'::jsonb, false))"
)


def is_system_notice_source(source: str, payload: Mapping[str, object] | None) -> bool:
    """Python twin of `SYSTEM_NOTICE_SOURCE`, kept adjacent on purpose — a
    drift between the two would reopen the gap from opposite sides; the parity
    test fails if they disagree on any `(source, payload)` input.

    Fail-closed on the recovery marker: only the exact JSON boolean `true`
    exempts; `payload.get(...) is not True` (never truthy — `1 == True` in
    Python) keeps the notice verdict for a missing key, JSON null, or any
    other value."""
    if not (source == "system" or source.startswith("system:")):
        return False
    if payload is None:
        return True
    return payload.get(HOSTED_TURN_RECOVERY_MARKER) is not True


# A closed agent (`agents_meta.closed_at` set) never auto-resurrects: every
# automatic resurrection path — delivery chat, compact, the delivery
# watchdog's terminated-owner retry, hosted-turn recovery — skips it, so its
# queued work stays pending and dead-letters on the existing thresholds. The
# user closes an agent with `terminate --final`; only an explicit manual
# resurrect reopens it (clearing the column). This is the closure analogue
# of the notice gate above: the delivery watchdog's selection and the
# resurrect endpoint (plus the home runner's final CAS) gate the same
# decision and must be updated together (the parity test pins the SQL
# fragment and the Python twin to each other).
CLOSED_AGENT: LiteralString = "agents_meta.closed_at IS NOT NULL"


def is_closed_agent(closed_at: datetime | None) -> bool:
    """Python twin of `CLOSED_AGENT`, kept adjacent on purpose — a drift
    between the two would reopen the closure gap from opposite sides; the
    parity test fails if they disagree on any `closed_at` input."""
    return closed_at is not None


# Every unapplied lifecycle command whose intent predates the recorded
# resurrection is closed by it, visibly (the payload names the resurrect inbound
# that superseded it), never silently dropped. Applied commands are preserved:
# an in-flight external effect cannot be undone, and no observation timestamp is
# invented for a command that never ran.
_SUPERSEDED_BY_RESURRECT: LiteralString = (
    "UPDATE inbound_messages i SET status='done', "
    "payload=COALESCE(i.payload,'{}'::jsonb)||jsonb_build_object('lifecycle_result',"
    "jsonb_build_object('outcome','superseded','reason','resurrect',"
    "'resurrect_inbound_id',m.last_resurrect_inbound_id)) "
    "FROM agents_meta m "
    "WHERE i.agent_id=%s AND m.id=i.agent_id AND m.last_resurrect_inbound_id IS NOT NULL "
    "AND i.kind IN ('restart','terminate') AND i.status IN ('pending','claimed') "
    "AND i.applied_at IS NULL AND i.id < m.last_resurrect_inbound_id "
    "RETURNING i.id"
)


@dataclass(frozen=True)
class LifecycleIntent:
    id: int
    agent_id: int
    kind: str
    generation: UUID
    owner: UUID
    accepted_at: datetime


_ACCEPT = """
WITH target AS MATERIALIZED (
 SELECT id,lifecycle_command_id,runtime_generation,runtime_owner,last_resurrect_inbound_id
 FROM agents_meta
 WHERE id=%s AND runtime_generation=%s AND runtime_owner=%s FOR UPDATE
), pending AS (
 SELECT i.id FROM inbound_messages i JOIN target t ON t.id=i.agent_id
 WHERE t.lifecycle_command_id IS NULL AND i.status='pending'
 AND i.kind IN ('restart','terminate')
 AND i.id > COALESCE(t.last_resurrect_inbound_id, 0)
 ORDER BY i.id LIMIT 1 FOR UPDATE OF i
), accepted AS (
 UPDATE inbound_messages i SET status='claimed',claimed_at=clock_timestamp(),
 target_generation=t.runtime_generation,target_owner=t.runtime_owner
 FROM pending p,target t WHERE i.id=p.id AND i.target_generation IS NULL AND i.target_owner IS NULL
 RETURNING i.id,i.agent_id,i.kind,i.target_generation,i.target_owner,i.claimed_at
), pointer AS (
 UPDATE agents_meta m SET lifecycle_command_id=a.id FROM accepted a
 WHERE m.id=a.agent_id AND m.lifecycle_command_id IS NULL RETURNING m.id
), chosen AS (
 SELECT i.id,i.agent_id,i.kind,i.target_generation,i.target_owner,i.claimed_at
 FROM target t JOIN inbound_messages i ON i.id=t.lifecycle_command_id AND i.agent_id=t.id
 WHERE i.status='claimed'
 UNION ALL SELECT * FROM accepted
)
SELECT t.lifecycle_command_id,c.*,(SELECT count(*) FROM pending)
FROM target t LEFT JOIN chosen c ON c.agent_id=t.id
CROSS JOIN (SELECT count(*) FROM pointer) written
"""


def _decode(row: tuple[Any, ...] | None) -> LifecycleIntent | None:
    if row is None:
        raise RuntimeError("lifecycle acceptance target incarnation changed")
    if row[1] is None:
        if row[0] is not None:
            raise RuntimeError("lifecycle pointer does not reference an unfinished command")
        if row[7]:
            raise RuntimeError("pending lifecycle request already carries a target")
        return None
    return LifecycleIntent(*row[1:7])


def accept_lifecycle_command(
    conn: psycopg.Connection, target: RuntimeIncarnation
) -> LifecycleIntent | None:
    """Caller retains its ownership/absence proof lock through this write."""
    if conn.info.transaction_status != TransactionStatus.INTRANS:
        raise RuntimeError("lifecycle acceptance requires an explicit transaction")
    _settle_superseded_by_resurrect(conn, target.agent_id)
    return _decode(
        conn.execute(_ACCEPT, (target.agent_id, target.generation, target.owner)).fetchone()
    )


async def accept_lifecycle_command_async(
    conn: psycopg.AsyncConnection, target: RuntimeIncarnation
) -> LifecycleIntent | None:
    """Async transport for the same SQL writer; no alternate admission rules."""
    if conn.info.transaction_status != TransactionStatus.INTRANS:
        raise RuntimeError("lifecycle acceptance requires an explicit transaction")
    # The fence is also on the selection below; settling first closes rows the
    # resurrect transaction could not see (they committed after it) so pending
    # counts and the unfinished-command pointer stay consistent in this pass.
    await _settle_superseded_by_resurrect_async(conn, target.agent_id)
    cursor = await conn.execute(_ACCEPT, (target.agent_id, target.generation, target.owner))
    return _decode(await cursor.fetchone())


def _settle_superseded_by_resurrect(conn: psycopg.Connection, agent_id: int) -> None:
    """Close every unapplied command below the agent's resurrection fence.

    The caller holds the agents_meta row lock, so the fence it reads is stable.
    A pointer to a command this settles is cleared in the same transaction: an
    unfinished-command pointer must never outlive the command it names.
    """
    rows = conn.execute(_SUPERSEDED_BY_RESURRECT, (agent_id,)).fetchall()
    if rows:
        conn.execute(
            "UPDATE agents_meta SET lifecycle_command_id=NULL WHERE id=%s "
            "AND lifecycle_command_id=ANY(%s)",
            (agent_id, [row[0] for row in rows]),
        )


async def _settle_superseded_by_resurrect_async(
    conn: psycopg.AsyncConnection, agent_id: int
) -> None:
    cursor = await conn.execute(_SUPERSEDED_BY_RESURRECT, (agent_id,))
    rows = await cursor.fetchall()
    if rows:
        await conn.execute(
            "UPDATE agents_meta SET lifecycle_command_id=NULL WHERE id=%s "
            "AND lifecycle_command_id=ANY(%s)",
            (agent_id, [row[0] for row in rows]),
        )


def supersede_lifecycle_for_resurrect(
    conn: psycopg.Connection, agent_id: int, resurrect_id: int
) -> None:
    """Record a resurrection's epoch fence, then settle every earlier command.

    Called by the resurrection transaction under the agents_meta row lock and
    before its observation check: a command that never applied cannot defer the
    new incarnation, and must not be able to kill it after admission either. A
    command created after the resurrect inbound stays current intent and is left
    alone.
    """
    if conn.info.transaction_status != TransactionStatus.INTRANS:
        raise RuntimeError("resurrect supersession requires an explicit transaction")
    conn.execute(
        "UPDATE agents_meta SET last_resurrect_inbound_id=%s WHERE id=%s",
        (resurrect_id, agent_id),
    )
    _settle_superseded_by_resurrect(conn, agent_id)


def supersede_lifecycle_for_force(conn: psycopg.Connection, agent_id: int, force_id: int) -> None:
    """The existing explicit force fence cancels earlier commands, not their history.

    Applied is preserved: an in-flight external effect cannot be undone. No
    observation timestamp is invented. The new terminate is left for actual
    settlement; ordinary chat remains pending behind its existing force cutoff.
    """
    if conn.info.transaction_status != TransactionStatus.INTRANS:
        raise RuntimeError("force lifecycle settlement requires an explicit transaction")
    row = conn.execute(
        "SELECT lifecycle_command_id,last_force_terminate_inbound_id FROM agents_meta "
        "WHERE id=%s FOR UPDATE",
        (agent_id,),
    ).fetchone()
    if row is None or row[1] != force_id:
        raise RuntimeError("force lifecycle settlement lost its intent fence")
    if row[0] is not None and row[0] >= force_id:
        raise RuntimeError("force lifecycle settlement cannot cancel a later pointer")
    conn.execute(
        "SELECT id FROM inbound_messages WHERE agent_id=%s AND id<%s "
        "AND kind IN ('restart','terminate') AND status IN ('pending','claimed') "
        "ORDER BY id FOR UPDATE",
        (agent_id, force_id),
    ).fetchall()
    conn.execute(
        "UPDATE inbound_messages SET status='done',payload=COALESCE(payload,'{}'::jsonb)||%s "
        "WHERE agent_id=%s AND id<%s AND kind IN ('restart','terminate') "
        "AND status IN ('pending','claimed')",
        (
            Jsonb({"lifecycle_result": {"outcome": "superseded", "reason": "force_terminate"}}),
            agent_id,
            force_id,
        ),
    )
    cleared = conn.execute(
        "UPDATE agents_meta SET lifecycle_command_id=NULL WHERE id=%s "
        "AND last_force_terminate_inbound_id=%s AND lifecycle_command_id IS NOT DISTINCT FROM %s",
        (agent_id, force_id, row[0]),
    )
    if cleared.rowcount != 1:
        raise RuntimeError("force lifecycle settlement lost its locked pointer")
