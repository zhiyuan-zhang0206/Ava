"""Transactional retry-launch intent: one operation, one immutable attempt."""

from uuid import uuid4

from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from base.agents import AgentNotFound
from base.db.transaction import write_transaction
from ops.rpc_schemas.launch_retry import RetryLaunchAccepted, RetryLaunchRequest


class RetryLaunchConflictError(ValueError):
    """The operation changed or its observed attempt is no longer retryable."""


def accept_retry_launch(
    pool: ConnectionPool, key: str, agent_id: int, body: RetryLaunchRequest
) -> tuple[str, RetryLaunchAccepted]:
    """Replay before mutable existence; commit the receipt and pointer together."""
    with write_transaction(pool) as conn, conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (key,))
        cur.execute(
            "SELECT agent_id, prior_attempt_id, machine, acceptance "
            "FROM agent_launch_retry_receipts WHERE operation_key=%s",
            (key,),
        )
        prior = cur.fetchone()
        if prior is not None:
            if prior[:2] != (agent_id, body.expected_prior_attempt_id):
                raise RetryLaunchConflictError(
                    "idempotency key reused with a different retry intent"
                )
            return prior[2], RetryLaunchAccepted.model_validate(prior[3])
        cur.execute(
            "SELECT machine, last_launch_attempt_id, status, last_admission_at, "
            "config_overlay, birth_config FROM agents_meta WHERE id=%s FOR UPDATE",
            (agent_id,),
        )
        row = cur.fetchone()
        if row is None:
            raise AgentNotFound(f"agent {agent_id} does not exist")
        machine, attempt, status, admitted_at, config, birth = row
        if (
            attempt != body.expected_prior_attempt_id
            or status != "idling"
            or admitted_at is not None
        ):
            raise RetryLaunchConflictError("observed launch attempt is stale or already admitted")
        new_attempt = uuid4()
        cur.execute("SELECT clock_timestamp()")
        clock_row = cur.fetchone()
        # SELECT without a filter always returns one row.
        assert clock_row is not None  # noqa: S101
        accepted_at = clock_row[0]
        accepted = RetryLaunchAccepted(
            agent_id=agent_id,
            prior_attempt_id=attempt,
            launch_attempt_id=new_attempt,
            accepted_at=accepted_at,
        )
        cur.execute(
            "INSERT INTO agent_launch_retry_receipts "
            "(operation_key, agent_id, prior_attempt_id, launch_attempt_id, machine, "
            "config_overlay, birth_config, acceptance) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                key,
                agent_id,
                attempt,
                new_attempt,
                machine,
                Jsonb(config),
                Jsonb(birth),
                Jsonb(accepted.model_dump(mode="json")),
            ),
        )
        cur.execute(
            "UPDATE agents_meta SET last_launch_attempt_id=%s WHERE id=%s", (new_attempt, agent_id)
        )
        return machine, accepted
