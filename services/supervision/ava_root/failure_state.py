"""Root unit failure states: the condition behind the `root_unit_failure_state` signal.

The health monitor derives one condition per unit each round — the unit's
intent is running and it sits in an explicit failure state: a recorded
replacement failure, an open restart breaker. The
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
    """A replacement failed at its down or up half; the monitor retries it."""

    BREAKER_OPEN = "breaker_open"
    """Repeated non-alive rounds opened the restart breaker; restarts are held."""


@dataclass(frozen=True, slots=True)
class UnitFailureFacts:
    """The supervisor-side facts of one unit (`Supervisor.unit_failure_facts`)."""

    intent_running: bool
    restart_failed: RestartFailure | None


@dataclass(frozen=True, slots=True)
class UnitFailureView:
    """One unit as the health monitor observed it this round."""

    unit: str
    facts: UnitFailureFacts
    breaker_open: bool
    detail: str = ""


def derive_kind(view: UnitFailureView) -> FailureKind | None:
    """The unit's failure state, or None while it is not failing.

    Only a unit whose intent is running can be failing; among its failure
    states, the recorded replacement failure wins over the open breaker, which
    follows it.
    """
    if not view.facts.intent_running:
        return None
    if view.facts.restart_failed is not None:
        return FailureKind.RESTART_FAILED
    if view.breaker_open:
        return FailureKind.BREAKER_OPEN
    return None


def describe(view: UnitFailureView, kind: FailureKind) -> str:
    """The state's one-line evidence for `kind`, from the view's facts."""
    failure = view.facts.restart_failed
    if kind is FailureKind.RESTART_FAILED and failure is not None:
        return f"replacement failed at its {failure.stage.value} half: {failure.detail}"
    if kind is FailureKind.BREAKER_OPEN:
        return view.detail or "restart breaker open"
    return view.detail
