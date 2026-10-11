"""Root unit failure states: the condition behind the `root_unit_failure_state` signal.

The health monitor derives one condition per unit each round — the unit's
intent is running and it sits in an explicit failure state: a recorded
replacement failure, exhausted automatic restart attempts. The
condition is a STATE, so the monitor emits it on every round while it holds;
the observability stack's rules read that stream (the pending period carries
the debounce, silence after the last round resolves it).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from services.supervision.ava_root.intent_store import RestartFailure


class FailureKind(StrEnum):
    """The failure states a unit can sit in (derivation order in `derive_kind`)."""

    RESTART_FAILED = "restart_failed"
    """A replacement failed at its down or up half; the monitor may retry within its finite budget."""

    RESTARTS_EXHAUSTED = "restarts_exhausted"
    """The finite automatic restart budget is spent; an operator must act."""


@dataclass(frozen=True, slots=True)
class UnitFailureFacts:
    """The supervisor-side facts of one unit (`Supervisor.unit_failure_facts`)."""

    intent_running: bool
    restart_failed: RestartFailure | None


def derive_kind(facts: UnitFailureFacts, *, restart_limit_reached: bool) -> FailureKind | None:
    """Only running intent can fail; recorded replacement errors take precedence."""
    if not facts.intent_running:
        return None
    if facts.restart_failed is not None:
        return FailureKind.RESTART_FAILED
    if restart_limit_reached:
        return FailureKind.RESTARTS_EXHAUSTED
    return None


def describe(facts: UnitFailureFacts, kind: FailureKind, detail: str = "") -> str:
    """One line of observed evidence for the unit's failure kind."""
    failure = facts.restart_failed
    if kind is FailureKind.RESTART_FAILED and failure is not None:
        return f"replacement failed at its {failure.stage.value} half: {failure.detail}"
    if kind is FailureKind.RESTARTS_EXHAUSTED:
        return detail or "automatic restart attempts exhausted"
    return detail
