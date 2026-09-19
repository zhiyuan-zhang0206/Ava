"""Lock the actual target incarnation before writing new-format chat inbounds."""

import psycopg
from psycopg.pq import TransactionStatus

from shared.caller_identity import PREFIXES, CallerIdentity
from shared.runtime_incarnation import RUNTIME_PROTOCOL_V1, RuntimeIncarnation


class CallerProtocolUnavailableError(ValueError):
    """The live consumer has not positively advertised this inbound protocol."""


# Every refusal keeps the original safety clause verbatim: a refused caller is
# never to be re-labeled as the user, system, or another Ava agent.
_NO_SUBSTITUTE = "do not substitute user/system/agent"


def require_caller_protocol(
    conn: psycopg.Connection, agent_id: int, source: str
) -> RuntimeIncarnation | None:
    """Hold ownership through INSERT; no installed-SHA or host-wide shortcuts.

    Runtime admission currently advertises zero. A future activation must first
    prove the old-writer upgrade barrier; this helper cannot establish it from
    code presence. A terminated/new target has no consumer and is refused.

    A refusal names the single unmet admission condition with its current value
    and the repair step (see `_refusal_message`), instead of the former copy
    that listed every condition at once and left the caller unable to tell
    which one failed.
    """
    if not source.startswith(PREFIXES):
        return None
    CallerIdentity.from_source(source)
    if conn.info.transaction_status != TransactionStatus.INTRANS:
        raise RuntimeError("caller protocol gate requires the inbound INSERT transaction")
    # Acquire first: a SELECT ... WHERE lease > clock_timestamp() FOR UPDATE
    # can evaluate the predicate before waiting on an unchanged locked tuple.
    # Even wall-clock time must be sampled after that wait, not merely after BEGIN.
    conn.execute("SELECT id FROM agents_meta WHERE id = %s FOR UPDATE", (agent_id,)).fetchone()
    row = conn.execute(
        "SELECT runtime_generation, runtime_owner FROM agents_meta "
        "WHERE id = %s AND status IN ('running', 'idling') "
        "AND runtime_kind IN ('process', 'hosted') "
        "AND runtime_generation IS NOT NULL AND runtime_owner IS NOT NULL "
        "AND runtime_protocol_version >= %s AND lease_expires_at > clock_timestamp()",
        (agent_id, RUNTIME_PROTOCOL_V1),
    ).fetchone()
    if row is None:
        raise CallerProtocolUnavailableError(_refusal_message(conn, agent_id))
    return RuntimeIncarnation(agent_id, row[0], row[1])


def _refusal_message(conn: psycopg.Connection, agent_id: int) -> str:
    """Name the single unmet admission condition, its current value and repair.

    Failure path only; the passing path never runs this read. It uses the
    caller's connection inside the same transaction -- the `FOR UPDATE`
    acquisition above already holds the row lock when the row exists -- so it
    opens no second connection, takes no additional lock, and cannot observe a
    state other than the one the gate just refused. (A row that did not exist
    to lock can, in principle, appear ready between the two statements; the
    fallback refusal below covers that race without claiming a condition.)

    Conditions are checked in this fixed order, and only the first unmet one is
    reported, so a refusal always names exactly one condition:
    missing row -> status -> runtime kind -> admitted generation/owner ->
    protocol activation -> lease freshness.
    Structural facts rank above the volatile lease (a lease renews on its own,
    absent admission does not), and protocol ranks above the lease so a runtime
    still advertising 0 reports "not activated" -- the durable, diagnosable
    state -- rather than a transient expired lease.
    """
    row = conn.execute(
        "SELECT status, runtime_kind, runtime_generation, runtime_owner, "
        "runtime_protocol_version, lease_expires_at, clock_timestamp() "
        "FROM agents_meta WHERE id = %s",
        (agent_id,),
    ).fetchone()
    if row is None:
        return (
            "target runtime protocol v1: no agents_meta row for the target; "
            f"create or start the target, then retry; {_NO_SUBSTITUTE}"
        )
    (status, kind, generation, owner, version, lease_expires_at, now) = row
    if status not in ("running", "idling"):
        return (
            f"target runtime protocol v1: status is {_shown(status)} "
            f"(requires running or idling); start or revive the target, then retry; "
            f"{_NO_SUBSTITUTE}"
        )
    if kind not in ("process", "hosted"):
        return (
            f"target runtime protocol v1: runtime_kind is {_shown(kind)} "
            f"(requires process or hosted); admit or restart the target runtime, then retry; "
            f"{_NO_SUBSTITUTE}"
        )
    missing = [
        name
        for name, value in (("runtime_generation", generation), ("runtime_owner", owner))
        if value is None
    ]
    if missing:
        condition = " and ".join(f"{name} is NULL" for name in missing)
        return (
            f"target runtime protocol v1: {condition} (a completed admission records both); "
            f"admit or restart the target runtime, then retry; {_NO_SUBSTITUTE}"
        )
    if version is None or version < RUNTIME_PROTOCOL_V1:
        return (
            f"target runtime protocol v1: runtime_protocol_version is {_shown(version)} "
            f"(requires >= {RUNTIME_PROTOCOL_V1}); the target runtime has not activated "
            f"protocol v1 yet, so an unchanged retry cannot succeed until the runtime is "
            f"activated for v1; "
            f"{_NO_SUBSTITUTE}"
        )
    if lease_expires_at is None or lease_expires_at <= now:
        lease = "NULL (no lease; must be in the future)"
        if lease_expires_at is not None:
            stamp = lease_expires_at.isoformat(sep=" ", timespec="seconds")
            lease = f"{stamp} (expired; must be in the future)"
        return (
            f"target runtime protocol v1: lease_expires_at is {lease}; "
            f"the target runtime must re-admit a fresh lease, then retry; {_NO_SUBSTITUTE}"
        )
    # Nothing above matched: the row did not exist to lock and changed between
    # the predicate and this read. Keep the former all-conditions refusal
    # rather than claim a condition the re-read cannot confirm.
    return (
        "target runtime protocol v1 requires a current generation, owner and fresh lease; "
        "upgrade/admit the target after the old-writer barrier, then retry; "
        f"{_NO_SUBSTITUTE}"
    )


def _shown(value: object) -> str:
    """Render one column value for the refusal text; SQL NULL reads as NULL."""
    return "NULL" if value is None else repr(value)
