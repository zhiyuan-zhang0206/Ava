"""Billing batch-recovery wire models — the `POST /api/agents/resurrect-billing`
request/response and its per-agent `resurrect-billing-v1` home action (task #3919).

The `ops.rpc_schemas` door re-exports these names.
"""

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel


class BillingRecoveryHomeResult(StrEnum):
    """One home runner's adjudication, before batch-summary translation."""

    SPAWNED = "spawned"
    ALREADY_ALIVE = "already_alive"
    REFUSED = "refused"
    DEFERRED = "deferred"


class BillingRecoveryOutcome(StrEnum):
    """One agent's outcome in the operator's billing recovery batch."""

    CANDIDATE = "candidate"
    RESURRECTED = "resurrected"
    ALREADY_ALIVE = "already_alive"
    REFUSED = "refused"
    DEFERRED = "deferred"
    FAILED = "failed"


class BillingResurrectRequest(BaseModel):
    """POST /api/agents/resurrect-billing request body — the explicit,
    operator-triggered billing batch-recovery entry (task #3919).

    `execute=False` (the default) is a strictly read-only preview: the
    billing-class halt candidates, the halted-but-alive survey, and the
    provider balance readout, nothing written. `execute=True` re-checks the
    provider balance and, when the gate passes, resurrects each candidate on
    its home machine through the versioned `resurrect-billing-v1` op.
    """

    execute: bool = False


class BillingBalanceReport(BaseModel):
    """Provider balance probe result carried on every billing-recovery response.

    `ok=False` always carries the human-readable `detail`; the run refuses
    execution on it (fail closed).
    """

    ok: bool
    detail: str
    threshold: float
    total: float | None = None
    currency: str | None = None


class BillingResurrectAgentOutcome(BaseModel):
    """One agent's line in the billing batch-recovery summary.

    `candidate` (dry-run listing) / `resurrected` / `already_alive` /
    `refused` / `deferred` / `failed`; `reason` carries the guard or failure
    detail where one exists.
    """

    agent_id: int
    machine: str
    status: BillingRecoveryOutcome
    reason: str | None = None


class BillingResurrectAgentResponse(BaseModel):
    """Home-runner adjudication of `resurrect-billing-v1` for one agent.

    `spawned`: every billing-victim guard passed under the row lock;
        terminated -> idling + host wake.
    `already_alive`: not terminated — an idempotent repeat, or a concurrent
        run won (the per-agent CAS is the dedupe).
    `refused`: a guard refused (fail closed) — `reason` names it
        ('not_billing_halted', 'machine_paused', 'runtime_cutover_required').
    `deferred`: the outstanding hosted lifecycle command has not settled
        (`ResurrectSettlementDeferredError`); retry once it settles.
    """

    status: BillingRecoveryHomeResult
    reason: str | None = None


class BillingHaltedAliveRow(BaseModel):
    """One billing-halted agent that is still alive — reported, never actioned.

    The entry only resurrects terminated victims. A halted agent that is still
    alive parks heartbeats and clears its own halt on the next successful turn,
    so the batch lists these rows for operator visibility and deliberately
    takes no action on them; releasing their hold is a follow-up candidate,
    not part of this entry.
    """

    agent_id: int
    machine: str
    streak: int


class BillingResurrectResponse(BaseModel):
    """POST /api/agents/resurrect-billing response — the run-level summary.

    `preview`: dry-run. `executed`: the run acted (a no-op when the whitelist
    was already empty — also audited). `refused`: the run did not act;
    `refusal_reason` names the gate ('balance gate not satisfied: ...' or a
    concurrent run holding the single-flight lock).

    `agents` carries the candidates (preview) or the per-agent outcomes;
    `halted_alive` is the report-only survey of billing-halted rows that are
    still alive — never actioned.
    """

    mode: Literal["dry_run", "execute"]
    outcome: Literal["preview", "executed", "refused"]
    refusal_reason: str | None = None
    balance: BillingBalanceReport
    agents: list[BillingResurrectAgentOutcome]
    halted_alive: list[BillingHaltedAliveRow]
