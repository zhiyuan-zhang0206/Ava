"""Gateway Loki-budget singleton and telemetry adapter."""

from __future__ import annotations

from shared import telemetry
from shared.loki_index_labels import LOKI_QUERY_CONCURRENCY
from shared.loki_query_budget import (
    BudgetErrorFactory,
    BudgetMetrics,
    BudgetObservation,
    BudgetObserver,
    BudgetOutcome,
    BudgetRejectReason,
    FairQueryBudget,
    LokiQueryBudgetError,
)

# Every gateway Loki read funnels through gateway.loki_events._get_json and
# therefore this process singleton. The daemon owns a separate lower-capacity
# instance because it runs in another process.
LOKI_QUERY_MAX_WAITERS = 128
LOKI_QUERY_WAIT_TIMEOUT_S = 10.0


def _emit_observation(observation: BudgetObservation) -> None:
    """Enqueue one observation without DB or Loki I/O on this caller."""
    telemetry.emit(
        "telemetry",
        "loki_query_budget",
        attributes={
            "outcome": observation.outcome,
            "active": observation.active,
            "queued": observation.queued,
            "high_water": observation.high_water,
            "wait_ms": observation.wait_ms,
            "acquired": observation.acquired,
            "queue_full": observation.queue_full,
            "wait_timeout": observation.wait_timeout,
        },
    )


query_budget = FairQueryBudget(
    capacity=LOKI_QUERY_CONCURRENCY,
    max_waiters=LOKI_QUERY_MAX_WAITERS,
    wait_timeout_s=LOKI_QUERY_WAIT_TIMEOUT_S,
    observer=_emit_observation,
)


def reset_for_tests(
    *,
    capacity: int = LOKI_QUERY_CONCURRENCY,
    max_waiters: int = LOKI_QUERY_MAX_WAITERS,
    wait_timeout_s: float = LOKI_QUERY_WAIT_TIMEOUT_S,
    observer: BudgetObserver | None = None,
    error_factory: BudgetErrorFactory = LokiQueryBudgetError,
) -> None:
    """Replace the process budget between isolated unit tests."""
    global query_budget  # noqa: PLW0603 — intentional process singleton test seam
    query_budget = FairQueryBudget(
        capacity=capacity,
        max_waiters=max_waiters,
        wait_timeout_s=wait_timeout_s,
        observer=observer,
        error_factory=error_factory,
    )


__all__ = [
    "LOKI_QUERY_CONCURRENCY",
    "LOKI_QUERY_MAX_WAITERS",
    "LOKI_QUERY_WAIT_TIMEOUT_S",
    "BudgetErrorFactory",
    "BudgetMetrics",
    "BudgetObservation",
    "BudgetObserver",
    "BudgetOutcome",
    "BudgetRejectReason",
    "FairQueryBudget",
    "LokiQueryBudgetError",
    "query_budget",
    "reset_for_tests",
]
