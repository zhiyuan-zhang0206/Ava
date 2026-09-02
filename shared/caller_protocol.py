"""Lock the actual target incarnation before writing new-format chat inbounds."""

import psycopg
from psycopg.pq import TransactionStatus

from shared.caller_identity import PREFIXES, CallerIdentity
from shared.runtime_incarnation import RuntimeIncarnation


class CallerProtocolUnavailableError(ValueError):
    """The live consumer has not positively advertised this inbound protocol."""


def require_caller_protocol(
    conn: psycopg.Connection, agent_id: int, source: str
) -> RuntimeIncarnation | None:
    """Hold ownership through INSERT; no installed-SHA or host-wide shortcuts.

    Runtime admission currently advertises zero. A future activation must first
    prove the old-writer upgrade barrier; this helper cannot establish it from
    code presence. A terminated/new target has no consumer and is refused.
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
        "AND runtime_protocol_version >= 1 AND lease_expires_at > clock_timestamp()",
        (agent_id,),
    ).fetchone()
    if row is None:
        raise CallerProtocolUnavailableError(
            "target runtime protocol v1 requires a current generation, owner and fresh lease; "
            "upgrade/admit the target after the old-writer barrier, then retry; "
            "do not substitute user/system/agent"
        )
    return RuntimeIncarnation(agent_id, row[0], row[1])
