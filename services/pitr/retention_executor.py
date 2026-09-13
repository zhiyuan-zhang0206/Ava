"""One retention execution tick: identity-bound deletes under hard limits.

The executor is the only code path that may delete policy-owned objects. It
runs exactly one digest-approved plan: any plan blocker or a digest that
differs from the caller's approved digest refuses the whole tick, nothing is
deleted. Eligible decisions are walked in their canonical order and every
tick is bounded by object count, deleted bytes (a fraction of the remote
total), and a delete rate. Each attempt is journalled intent-first; a failed
object never aborts the tick and is retried by the next tick's fresh plan
(design sections 3.4 and 4).

The deletion unit is the host object plus its sidecar (design section 2.5):
a sidecar is attempted only after its host was deleted and re-observed
absent, so an interruption leaves at most a harmless orphan sidecar -- never
an evidence-less host object. Sidecars whose host was already gone arrive as
``plan.orphan_sidecars`` with their own recorded identity and are deleted
through the same identity-bound protocol after the decisions.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

from services.pitr.retention_delete import DeleteOutcome, RetentionDeleteStore
from services.pitr.retention_journal import RetentionJournal
from services.pitr.retention_manifest import RetentionPlan, RetentionSidecar


@dataclass(frozen=True)
class RetentionExecutionLimits:
    """Per-tick safety bounds; a single object above the byte budget waits."""

    max_objects: int = 256
    max_bytes_fraction: float = 0.05
    rate_per_second: float = 2.0


_DEFAULT_LIMITS = RetentionExecutionLimits()


@dataclass(frozen=True)
class RetentionExecutionSummary:
    """One tick's outcome counts, from the journal's point of view.

    The object counters cover host decisions and orphan sidecar units; a
    pair's sidecar rides inside its host unit and is counted separately.
    For a sidecar, ``absent`` means the sidecar was already gone (goal
    state); any other non-deleted outcome counts as failed.
    """

    plan_digest: str
    refused_reason: str | None
    attempted: int
    deleted: int
    absent: int
    mismatched: int
    failed: int
    verify_failed: int
    skipped: int
    sidecars_deleted: int = 0
    sidecars_absent: int = 0
    sidecars_failed: int = 0
    orphans_deleted: int = 0
    orphans_absent: int = 0
    orphans_failed: int = 0


def execute_retention_plan(  # noqa: PLR0915
    plan: RetentionPlan,
    *,
    expected_digest: str,
    delete_store: RetentionDeleteStore,
    verify_absent: Callable[[str], bool],
    remote_total_bytes: int,
    journal: RetentionJournal,
    limits: RetentionExecutionLimits = _DEFAULT_LIMITS,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> RetentionExecutionSummary:
    """Delete this plan's eligible objects, refusing on any doubt.

    ``expected_digest`` is the digest the operator approved for this plan;
    ``verify_absent`` re-observes one object and reports whether it is gone
    (the executor calls it after ``DELETED`` — a still-visible object is
    recorded as ``verify-failed``, never retried within the tick).

    A decision's sidecar is attempted only after the host deletion was
    re-observed absent; orphan sidecar units are gated by the same object
    and byte budgets as host decisions, then processed after them.
    """

    digest = plan.digest()
    refused: str | None = None
    if digest != expected_digest:
        refused = "plan digest differs from the approved digest"
    elif plan.blocked_reasons:
        refused = "plan carries blockers; eligibility is forced empty"
    if refused is not None:
        journal.append("refused", {"plan_digest": digest, "reason": refused})
        return RetentionExecutionSummary(digest, refused, 0, 0, 0, 0, 0, 0, 0)

    max_bytes = int(remote_total_bytes * limits.max_bytes_fraction)
    attempted = deleted = absent = mismatched = failed = verify_failed = 0
    deleted_bytes = 0
    skipped = 0
    sidecars_deleted = sidecars_absent = sidecars_failed = 0
    orphans_deleted = orphans_absent = orphans_failed = 0
    last_attempt: float | None = None

    def await_slot() -> None:
        nonlocal last_attempt
        if last_attempt is not None:
            wait = (1.0 / limits.rate_per_second) - (clock() - last_attempt)
            if wait > 0:
                sleep(wait)
        last_attempt = clock()

    def run_sidecar_delete(sidecar: RetentionSidecar, *, unit: str) -> str:
        """Delete one sidecar through the shared identity-bound protocol."""

        await_slot()
        journal.append(
            "intent",
            {
                "plan_digest": digest,
                "unit": unit,
                "object_name": sidecar.object_name,
                "identity": sidecar.pin_token,
                "size": sidecar.size,
            },
        )
        try:
            outcome = delete_store.delete_if_match(sidecar.object_name, sidecar.pin_token)
        except Exception as exc:  # per-object isolation is the contract
            journal.append(
                "result",
                {
                    "plan_digest": digest,
                    "unit": unit,
                    "object_name": sidecar.object_name,
                    "outcome": "delete-failed",
                    "error": f"{type(exc).__name__}: {exc}",
                },
            )
            return "delete-failed"
        if outcome is DeleteOutcome.DELETED:
            if verify_absent(sidecar.object_name):
                journal.append(
                    "result",
                    {
                        "plan_digest": digest,
                        "unit": unit,
                        "object_name": sidecar.object_name,
                        "outcome": "deleted",
                    },
                )
                return "deleted"
            journal.append(
                "result",
                {
                    "plan_digest": digest,
                    "unit": unit,
                    "object_name": sidecar.object_name,
                    "outcome": "verify-failed",
                },
            )
            return "verify-failed"
        if outcome is DeleteOutcome.ABSENT:
            journal.append(
                "result",
                {
                    "plan_digest": digest,
                    "unit": unit,
                    "object_name": sidecar.object_name,
                    "outcome": "absent",
                },
            )
            return "absent"
        journal.append(
            "result",
            {
                "plan_digest": digest,
                "unit": unit,
                "object_name": sidecar.object_name,
                "outcome": "identity-mismatch",
            },
        )
        return "identity-mismatch"

    for index, decision in enumerate(plan.eligible):
        item = decision.object
        over_objects = attempted >= limits.max_objects
        over_bytes = deleted_bytes + item.size > max_bytes
        if over_objects or over_bytes:
            skipped = len(plan.eligible) - index
            break
        await_slot()
        attempted += 1
        journal.append(
            "intent",
            {
                "plan_digest": digest,
                "object_name": item.object_name,
                "identity": item.pin_token,
                "size": item.size,
                "reason": decision.reason,
            },
        )
        try:
            outcome = delete_store.delete_if_match(item.object_name, item.pin_token)
        except Exception as exc:  # per-object isolation is the contract
            failed += 1
            journal.append(
                "result",
                {
                    "plan_digest": digest,
                    "object_name": item.object_name,
                    "outcome": "delete-failed",
                    "error": f"{type(exc).__name__}: {exc}",
                },
            )
            continue
        if outcome is DeleteOutcome.DELETED:
            if verify_absent(item.object_name):
                deleted += 1
                deleted_bytes += item.size
                journal.append(
                    "result",
                    {"plan_digest": digest, "object_name": item.object_name, "outcome": "deleted"},
                )
                if decision.sidecar is not None:
                    sidecar_outcome = run_sidecar_delete(decision.sidecar, unit="sidecar")
                    if sidecar_outcome == "deleted":
                        sidecars_deleted += 1
                        deleted_bytes += decision.sidecar.size
                    elif sidecar_outcome == "absent":
                        sidecars_absent += 1
                    else:
                        sidecars_failed += 1
            else:
                verify_failed += 1
                journal.append(
                    "result",
                    {
                        "plan_digest": digest,
                        "object_name": item.object_name,
                        "outcome": "verify-failed",
                    },
                )
        elif outcome is DeleteOutcome.ABSENT:
            absent += 1
            journal.append(
                "result",
                {"plan_digest": digest, "object_name": item.object_name, "outcome": "absent"},
            )
        else:
            mismatched += 1
            journal.append(
                "result",
                {
                    "plan_digest": digest,
                    "object_name": item.object_name,
                    "outcome": "identity-mismatch",
                },
            )
    for index, sidecar in enumerate(plan.orphan_sidecars):
        over_objects = attempted >= limits.max_objects
        over_bytes = deleted_bytes + sidecar.size > max_bytes
        if over_objects or over_bytes:
            skipped += len(plan.orphan_sidecars) - index
            break
        attempted += 1
        orphan_outcome = run_sidecar_delete(sidecar, unit="orphan-sidecar")
        if orphan_outcome == "deleted":
            orphans_deleted += 1
            deleted_bytes += sidecar.size
        elif orphan_outcome == "absent":
            orphans_absent += 1
        else:
            orphans_failed += 1
    if attempted:
        journal.append(
            "tick",
            {
                "plan_digest": digest,
                "deleted": deleted,
                "absent": absent,
                "mismatched": mismatched,
                "failed": failed,
                "verify_failed": verify_failed,
                "skipped": skipped,
                "sidecars_deleted": sidecars_deleted,
                "sidecars_absent": sidecars_absent,
                "sidecars_failed": sidecars_failed,
                "orphans_deleted": orphans_deleted,
                "orphans_absent": orphans_absent,
                "orphans_failed": orphans_failed,
            },
        )
    return RetentionExecutionSummary(
        digest,
        None,
        attempted,
        deleted,
        absent,
        mismatched,
        failed,
        verify_failed,
        skipped,
        sidecars_deleted,
        sidecars_absent,
        sidecars_failed,
        orphans_deleted,
        orphans_absent,
        orphans_failed,
    )
