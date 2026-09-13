"""One retention execution tick: identity-bound deletes under hard limits.

The executor is the only code path that may delete policy-owned objects. It
runs exactly one digest-approved plan: any plan blocker or a digest that
differs from the caller's approved digest refuses the whole tick, nothing is
deleted. Eligible decisions are walked in their canonical order and every
tick is bounded by object count, deleted bytes (a fraction of the remote
total), and a delete rate. Each attempt is journalled intent-first; a failed
object never aborts the tick and is retried by the next tick's fresh plan
(design sections 3.4 and 4).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

from services.pitr.retention_delete import DeleteOutcome, RetentionDeleteStore
from services.pitr.retention_journal import RetentionJournal
from services.pitr.retention_manifest import RetentionPlan


@dataclass(frozen=True)
class RetentionExecutionLimits:
    """Per-tick safety bounds; a single object above the byte budget waits."""

    max_objects: int = 256
    max_bytes_fraction: float = 0.05
    rate_per_second: float = 2.0


_DEFAULT_LIMITS = RetentionExecutionLimits()


@dataclass(frozen=True)
class RetentionExecutionSummary:
    """One tick's outcome counts, from the journal's point of view."""

    plan_digest: str
    refused_reason: str | None
    attempted: int
    deleted: int
    absent: int
    mismatched: int
    failed: int
    verify_failed: int
    skipped: int


def execute_retention_plan(
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
    last_attempt: float | None = None
    for index, decision in enumerate(plan.eligible):
        item = decision.object
        over_objects = attempted >= limits.max_objects
        over_bytes = deleted_bytes + item.size > max_bytes
        if over_objects or over_bytes:
            skipped = len(plan.eligible) - index
            break
        if last_attempt is not None:
            wait = (1.0 / limits.rate_per_second) - (clock() - last_attempt)
            if wait > 0:
                sleep(wait)
        last_attempt = clock()
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
            },
        )
    return RetentionExecutionSummary(
        digest, None, attempted, deleted, absent, mismatched, failed, verify_failed, skipped
    )
