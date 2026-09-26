"""Billing batch recovery — the explicit, controlled entry that reinstates the
fleet after a provider billing stoppage (task #3919, parent #3916).

The 2026-09-18 outage left 23 agents terminated by a deepseek balance
exhaustion (HTTP 402 -> classify_error PERMANENT -> recovery-breaker halt ->
corpse reap); the only recovery was one manual ``ava agents resurrect`` per
agent. This module gives operations one command: enumerate the billing-class
halt victims, verify the provider balance recovered, and resurrect them.

Contract:

- Explicit operator trigger only. Nothing here runs on a schedule or from any
  automatic recovery path; the closure fence (#3911) and the ordinary
  terminated semantics are untouched.
- Whitelist: ``status='terminated'`` + ``closed_at IS NULL`` + the recovery
  breaker halted (``permanent_reject_streak >= threshold``) with
  ``last_permanent_reject_reason = 'billing'`` + not user/integrity-terminated
  (explicit-human finalizations and corrupt-history rows are never re-opened
  by the batch).
- Balance gate: execution refuses unless the provider balance endpoint
  reports the account available above the configured floor (fail closed;
  ``execute=False`` previews the same readout).
- Report-only survey: billing-halted rows that are still alive (idling/running)
  ride the response's ``halted_alive`` list — visibility only, never actioned
  (they park heartbeats and clear the halt on their next successful turn;
  releasing their hold is a follow-up candidate, not part of this entry).
- Idempotent: the whitelist self-clears after a run; the per-agent flip is
  the existing row-locked terminated->idling CAS (exactly one spawn); a
  run-level advisory lock refuses a concurrent second run instead of queueing.
- Audit: one run-level ``billing_resurrect`` audit + ``billing_resurrect_run``
  telemetry event, plus the per-agent ``resurrect`` events
  (``resurrected_by='user'``, payload ``via='billing_recovery'``).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Literal

import httpx
from psycopg import Connection
from psycopg_pool import ConnectionPool

from ops import cluster_rpc as _cluster_rpc
from ops.rpc_schemas import (
    BillingBalanceReport,
    BillingHaltedAliveRow,
    BillingResurrectAgentOutcome,
    BillingResurrectAgentResponse,
    BillingResurrectResponse,
)
from shared.machine import machine_name

_log = logging.getLogger(__name__)

# Advisory-lock key for the single-flight guard. Arbitrary but stable
# cluster-wide (ASCII "AVBR" = Ava Billing Recovery) — same construction as
# the migration lock's "AVMI" (shared/migrations.py).
_RUN_LOCK_KEY = 0x41564252

# The per-agent summary status; the whitelist listing also rides it ('candidate').
_OutcomeStatus = Literal[
    "candidate", "resurrected", "already_alive", "refused", "deferred", "failed"
]

# Home-runner adjudication -> run-summary status. Typed so pyright checks the
# literal-to-literal mapping instead of laundering it through `str`.
_DISPATCH_STATUS_MAP: dict[str, _OutcomeStatus] = {
    "spawned": "resurrected",
    "already_alive": "already_alive",
    "refused": "refused",
    "deferred": "deferred",
}


@dataclass(frozen=True)
class BillingCandidate:
    """One whitelist row: a billing-halted terminated agent awaiting rescue."""

    agent_id: int
    machine: str
    streak: int
    termination_source: str | None


@dataclass(frozen=True)
class BillingHaltedAlive:
    """One report-only row: a billing-halted agent that is still alive."""

    agent_id: int
    machine: str
    streak: int


def enumerate_candidates(conn: Connection) -> list[BillingCandidate]:
    """The whitelist query — read-only by contract (dry-run writes nothing).

    A read failure propagates: the caller treats it as an aborted run, never
    as an empty candidate set (the same fail-closed shape as
    ``ops/resurrect_gates``).
    """
    from shared.agents import TerminationSource
    from shared.recovery_breaker import (
        HALT_AFTER_CONSECUTIVE_PERMANENT_REJECTS,
        PERMANENT_REJECT_REASON_BILLING,
    )

    rows = conn.execute(
        "SELECT id, machine, permanent_reject_streak, termination_source "
        "FROM agents_meta "
        "WHERE status = 'terminated' "
        "  AND closed_at IS NULL "
        "  AND permanent_reject_streak >= %s "
        "  AND last_permanent_reject_reason = %s "
        "  AND COALESCE(termination_source, '') NOT IN (%s, %s) "
        "ORDER BY id",
        (
            HALT_AFTER_CONSECUTIVE_PERMANENT_REJECTS,
            PERMANENT_REJECT_REASON_BILLING,
            TerminationSource.USER.value,
            TerminationSource.INTEGRITY.value,
        ),
    ).fetchall()
    return [
        BillingCandidate(
            agent_id=int(row[0]),
            machine=str(row[1]),
            streak=int(row[2]),
            termination_source=None if row[3] is None else str(row[3]),
        )
        for row in rows
    ]


def enumerate_halted_alive(conn: Connection) -> list[BillingHaltedAlive]:
    """The report-only survey: billing-halted rows that are still alive.

    Read-only; these rows are never actioned by the batch — they park
    heartbeats and clear the halt on their next successful turn — but the
    response lists them so an operator sees the parked part of the cohort
    (a just-resurrected row stays listed until its first successful turn
    clears the streak).
    """
    from shared.recovery_breaker import (
        HALT_AFTER_CONSECUTIVE_PERMANENT_REJECTS,
        PERMANENT_REJECT_REASON_BILLING,
    )

    rows = conn.execute(
        "SELECT id, machine, permanent_reject_streak "
        "FROM agents_meta "
        "WHERE status IN ('idling', 'running') "
        "  AND closed_at IS NULL "
        "  AND permanent_reject_streak >= %s "
        "  AND last_permanent_reject_reason = %s "
        "ORDER BY id",
        (HALT_AFTER_CONSECUTIVE_PERMANENT_REJECTS, PERMANENT_REJECT_REASON_BILLING),
    ).fetchall()
    return [
        BillingHaltedAlive(agent_id=int(row[0]), machine=str(row[1]), streak=int(row[2]))
        for row in rows
    ]


def _provider_key() -> str | None:
    """The DeepSeek bearer for the balance probe — gateway-safe.

    The Settings value when this process holds it; otherwise this unit's
    ``.env`` file. The gateway profile pops agent-runner capability keys from
    os.environ (Task #856), so ``settings.lm.deepseek_api_key`` resolves None
    there while the cluster ``.env`` stays the authoritative source — the same
    fallback shape as ``shared/lm/factory.py::_ensure_provider_key``. The file
    read is the sanctioned gateway-side consumption path, registered in
    ``tests/shared/test_gateway_consumer_guard.py::_FALLBACK_CONSUMED_READS``.
    """
    from shared.config import field_alias, settings
    from shared.runtime_config import read_env_aliases

    key = settings.lm.deepseek_api_key
    if key is not None:
        return key.get_secret_value()
    return read_env_aliases().get(field_alias("deepseek_api_key")) or None


def fetch_provider_balance() -> BillingBalanceReport:
    """Probe the provider account balance; never raises (a fail-closed report).

    Reads ``settings.daemon.billing_recovery_*`` for the endpoint / timeout /
    floor and the DeepSeek bearer via ``_provider_key()`` (the Settings value,
    or the unit ``.env`` file under the gateway profile that pops provider
    keys). Any transport, HTTP, or payload surprise returns ``ok=False`` with
    the reason; the run refuses rather than acting on an unverified account.
    """
    from shared import http_dial
    from shared.config import settings

    threshold = float(settings.daemon.billing_recovery_min_balance)
    url = settings.daemon.billing_recovery_balance_url
    timeout_s = float(settings.daemon.billing_recovery_balance_timeout_s)
    bearer = _provider_key()
    if bearer is None:
        return BillingBalanceReport(
            ok=False, detail="deepseek_api_key is not configured", threshold=threshold
        )
    try:
        resp = http_dial.get(
            url,
            headers={"Authorization": f"Bearer {bearer}"},
            timeout=timeout_s,
        )
    except httpx.HTTPError as exc:
        return BillingBalanceReport(
            ok=False, detail=f"balance probe failed: {type(exc).__name__}", threshold=threshold
        )
    if resp.status_code != 200:
        return BillingBalanceReport(
            ok=False,
            detail=f"balance endpoint returned HTTP {resp.status_code}",
            threshold=threshold,
        )
    try:
        data = resp.json()
        best = max(data["balance_infos"], key=lambda info: float(info["total_balance"]))
        total = float(best["total_balance"])
        currency = best.get("currency")
        available = data["is_available"]
    except (KeyError, TypeError, ValueError) as exc:
        return BillingBalanceReport(
            ok=False,
            detail=f"unexpected balance payload: {type(exc).__name__}",
            threshold=threshold,
        )
    ok = available is True and total >= threshold
    detail = (
        f"is_available={available}; max total_balance={total}"
        + (f" {currency}" if currency else "")
        + f" (floor {threshold})"
    )
    return BillingBalanceReport(
        ok=ok, detail=detail, threshold=threshold, total=total, currency=currency
    )


async def run_billing_recovery(*, execute: bool, pool: ConnectionPool) -> BillingResurrectResponse:
    """Preview (``execute=False``, read-only) or run the batch rescue."""
    candidates = await asyncio.to_thread(_enumerate_blocking, pool)
    survey = await asyncio.to_thread(_enumerate_halted_alive_blocking, pool)
    halted_alive = [_halted_alive_row(agent) for agent in survey]
    balance = await asyncio.to_thread(fetch_provider_balance)
    if not execute:
        return BillingResurrectResponse(
            mode="dry_run",
            outcome="preview",
            balance=balance,
            agents=[_candidate_outcome(c) for c in candidates],
            halted_alive=halted_alive,
        )
    if not balance.ok:
        _log.info("billing recovery refused: balance gate not satisfied (%s)", balance.detail)
        return BillingResurrectResponse(
            mode="execute",
            outcome="refused",
            refusal_reason=f"balance gate not satisfied: {balance.detail}",
            balance=balance,
            agents=[_candidate_outcome(c) for c in candidates],
            halted_alive=halted_alive,
        )
    held, lock_conn = await asyncio.to_thread(_try_run_lock, pool)
    if not held:
        _log.info("billing recovery refused: another run holds the single-flight lock")
        return BillingResurrectResponse(
            mode="execute",
            outcome="refused",
            refusal_reason="another billing-recovery run is in progress",
            balance=balance,
            agents=[_candidate_outcome(c) for c in candidates],
            halted_alive=halted_alive,
        )
    assert lock_conn is not None  # noqa: S101 — held=True guarantees the connection
    try:
        # Re-enumerate under the lock: a concurrent runner (or an operator)
        # may have changed the set since the preflight read.
        candidates = await asyncio.to_thread(_enumerate_blocking, pool)
        outcomes = await _dispatch_all(candidates)
    finally:
        await asyncio.to_thread(_release_run_lock, pool, lock_conn)
    await asyncio.to_thread(_record_run_event, balance, outcomes)
    return BillingResurrectResponse(
        mode="execute",
        outcome="executed",
        balance=balance,
        agents=outcomes,
        halted_alive=halted_alive,
    )


async def resurrect_billing_agent_op(agent_id: int) -> BillingResurrectAgentResponse:
    """The versioned ``resurrect-billing-v1`` action (home runner), also used
    as the in-process fallback when the local ops server is unreachable."""
    from ops.agent_wake import resurrect_agent
    from ops.resurrection_retry import ResurrectSettlementDeferredError
    from shared.agents import MachinePaused, ResurrectAlreadyAlive, ResurrectRefused

    try:
        await asyncio.to_thread(
            resurrect_agent, agent_id, resurrected_by="user", billing_recovery=True
        )
    except ResurrectAlreadyAlive:
        return BillingResurrectAgentResponse(status="already_alive")
    except ResurrectRefused as exc:
        return BillingResurrectAgentResponse(status="refused", reason=exc.reason)
    except MachinePaused as exc:
        return BillingResurrectAgentResponse(status="refused", reason=f"machine_paused: {exc}")
    except ResurrectSettlementDeferredError as exc:
        return BillingResurrectAgentResponse(status="deferred", reason=str(exc))
    return BillingResurrectAgentResponse(status="spawned")


async def _dispatch_all(candidates: list[BillingCandidate]) -> list[BillingResurrectAgentOutcome]:
    from shared.config import settings

    sem = asyncio.Semaphore(int(settings.daemon.billing_recovery_dispatch_concurrency))

    async def _guarded(candidate: BillingCandidate) -> BillingResurrectAgentOutcome:
        async with sem:
            return await _dispatch_one(candidate)

    return await asyncio.gather(*(_guarded(c) for c in candidates))


async def _dispatch_one(candidate: BillingCandidate) -> BillingResurrectAgentOutcome:
    path = f"/api/agents/{candidate.agent_id}/resurrect-billing-v1"
    try:
        forwarded = await _cluster_rpc.dispatch_to_machine(
            target_machine=candidate.machine,
            kind="lifecycle",
            payload={"path": path, "body": {}},
        )
        response = BillingResurrectAgentResponse.model_validate(forwarded)
    except _cluster_rpc.ClusterOpUnreachable:
        if candidate.machine != machine_name():
            return _outcome(candidate, "failed", "home machine unreachable")
        # Local ops server unreachable (test / single-process): mirror
        # `resurrect_if_terminated` and fall back to the in-process op.
        try:
            response = await resurrect_billing_agent_op(candidate.agent_id)
        except Exception as exc:
            return _outcome(candidate, "failed", f"{type(exc).__name__}: {exc}")
    except Exception as exc:
        return _outcome(candidate, "failed", f"{type(exc).__name__}: {exc}")
    return _outcome(candidate, _DISPATCH_STATUS_MAP[response.status], response.reason)


def _enumerate_blocking(pool: ConnectionPool) -> list[BillingCandidate]:
    with pool.connection() as conn:
        return enumerate_candidates(conn)


def _enumerate_halted_alive_blocking(pool: ConnectionPool) -> list[BillingHaltedAlive]:
    with pool.connection() as conn:
        return enumerate_halted_alive(conn)


def _try_run_lock(pool: ConnectionPool) -> tuple[bool, Connection | None]:
    conn = pool.getconn()
    try:
        row = conn.execute("SELECT pg_try_advisory_lock(%s)", (_RUN_LOCK_KEY,)).fetchone()
        conn.rollback()
    except Exception:
        pool.putconn(conn)
        raise
    if row is not None and row[0] is True:
        return True, conn
    pool.putconn(conn)
    return False, None


def _release_run_lock(pool: ConnectionPool, conn: Connection) -> None:
    try:
        conn.execute("SELECT pg_advisory_unlock(%s)", (_RUN_LOCK_KEY,))
        conn.rollback()
    finally:
        pool.putconn(conn)


def _candidate_outcome(candidate: BillingCandidate) -> BillingResurrectAgentOutcome:
    return BillingResurrectAgentOutcome(
        agent_id=candidate.agent_id, machine=candidate.machine, status="candidate"
    )


def _halted_alive_row(row: BillingHaltedAlive) -> BillingHaltedAliveRow:
    return BillingHaltedAliveRow(agent_id=row.agent_id, machine=row.machine, streak=row.streak)


def _outcome(
    candidate: BillingCandidate, status: _OutcomeStatus, reason: str | None = None
) -> BillingResurrectAgentOutcome:
    return BillingResurrectAgentOutcome(
        agent_id=candidate.agent_id, machine=candidate.machine, status=status, reason=reason
    )


def _record_run_event(
    balance: BillingBalanceReport, outcomes: list[BillingResurrectAgentOutcome]
) -> None:
    from shared import telemetry
    from shared.audit_events import insert_event_log

    resurrected = [o.agent_id for o in outcomes if o.status == "resurrected"]
    refused = [o.agent_id for o in outcomes if o.status == "refused"]
    deferred = [o.agent_id for o in outcomes if o.status == "deferred"]
    failed = [o.agent_id for o in outcomes if o.status == "failed"]
    insert_event_log(
        event_type="billing_resurrect",
        agent_id=None,
        source="user",
        payload={
            "balance": balance.model_dump(),
            "candidates": [o.agent_id for o in outcomes],
            "resurrected": resurrected,
            "refused": refused,
            "deferred": deferred,
            "failed": failed,
        },
    )
    telemetry.emit(
        "telemetry",
        "billing_resurrect_run",
        level="info",
        agent_id=None,
        source="user",
        attributes={
            "candidates": len(outcomes),
            "resurrected": len(resurrected),
            "refused": len(refused),
            "deferred": len(deferred),
            "failed": len(failed),
        },
    )
