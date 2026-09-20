"""The managed-writer enable point: one mode decision per rollout (task #4121).

The rollout's managed-writer call sites (the begin / collect / commit wiring)
may run only under a single per-rollout decision, resolved once between Phase 0
and the first stop effect (``_run_gateway_orchestration_inner``):

- ``update_managed_writer`` false -> ``off``: every rollout runs the legacy flow
  exactly as before. The switch is read HERE only -- no call site reads it.
- true and both readiness guards met -> ``active``: the rollout may enter
  managed-writer mode.
- true but a guard missing or not-True -> ``blocked``: the rollout still runs
  the legacy flow, and the refusal is recorded explicitly -- rollout-log line,
  the rollout telemetry's ``managed_writer`` field, a ``managed_writer_blocked``
  event, and the ``ava cluster status`` bit -- never a silent degradation.

The guards are module-level declarations, each defined in the module whose
state it proves, and are not config/env-overridable (fail-closed: an absent or
non-True attribute refuses entry):

- ``CHECKED_ACTIVATION_READY`` in ``cli/commands/_update_normal_release.py``:
  lands with the checked-activation change that replaces the release fence
  (task #4117 S5).
- ``MANAGED_WRITER_WIRING_COMPLETE`` in ``cli/commands/_update_publication.py``:
  set by the final wiring slice (task #4122 E2) as the completion declaration
  in the module still describing its seats as inert.

Read-once semantics: `decide_managed_writer_mode` reads the switch at most once
per process and caches the decision; a re-entry reuses it, so every call site
consumes one consistent decision and nothing re-reads the switch mid-rollout.
A new rollout is a new process and re-reads the file-backed config -- that is
how a flip takes effect on the next update with no extra restart.

Mode transitions are not event-carried: no existing surface can carry "the
previously observed mode" without adding state (the pre-cutover events archive
was dropped with the LGTM cleanup — task #1281/#1823; the last-update row's ``log_path``
is overwritten by this rollout's ``begin_update`` before the read point;
previous-rollout log forensics is a rotation-prone file heuristic). The
rebuild chain is the audited config write (``env_write``: old and new value
plus the actor) plus the per-rollout telemetry ``managed_writer`` field (all
three states, including ``off``); ``managed_writer_blocked`` marks a refusal
and the ``ava cluster status`` bit shows the effective state.
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib import import_module
from typing import Literal

from shared.rollout_telemetry import record_managed_writer

ManagedWriterState = Literal["off", "active", "blocked"]

# (blocked reason, module path, attribute). Dependency order: the checked
# activation lands (S5) before the wiring completes (E2's final slice).
_GUARDS: tuple[tuple[str, str, str], ...] = (
    (
        "checked_activation_not_ready",
        "cli.commands._update_normal_release",
        "CHECKED_ACTIVATION_READY",
    ),
    (
        "wiring_incomplete",
        "cli.commands._update_publication",
        "MANAGED_WRITER_WIRING_COMPLETE",
    ),
)

_REASON_PROSE = {
    "checked_activation_not_ready": "checked activation not ready",
    "wiring_incomplete": "wiring incomplete",
}


@dataclass(frozen=True)
class ManagedWriterMode:
    """One rollout's resolved mode, plus why when blocked."""

    state: ManagedWriterState
    blocked_reasons: tuple[str, ...] = ()

    def describe(self) -> str:
        """One human phrase for the rollout log line and the status bit."""
        if self.state == "blocked":
            unmet = ", ".join(_REASON_PROSE[reason] for reason in self.blocked_reasons)
            return f"blocked — {unmet}; running the legacy flow"
        return self.state


def _config_enabled() -> bool:
    """The enable switch, read from the live config (the read-point seam)."""
    from shared.config import settings

    return settings.gateway.update_managed_writer


def _guard_ready(module_path: str, attribute: str) -> bool:
    """Whether a readiness guard exists and is exactly True (fail-closed).

    An ImportError counts as not-ready: a broken import must refuse the mode,
    never crash a rollout that asked for it.
    """
    try:
        module = import_module(module_path)
    except ImportError:
        return False
    return getattr(module, attribute, None) is True


def effective_managed_writer_mode() -> ManagedWriterMode:
    """Resolve the mode right now, fresh (no caching, no recording).

    For surfaces reporting the standing state (``ava cluster status``); the
    rollout goes through `decide_managed_writer_mode` so its single decision is
    recorded as evidence.
    """
    if not _config_enabled():
        return ManagedWriterMode("off")
    unmet = tuple(
        reason
        for reason, module_path, attribute in _GUARDS
        if not _guard_ready(module_path, attribute)
    )
    return ManagedWriterMode("blocked", unmet) if unmet else ManagedWriterMode("active")


class _DecisionSlot:
    """Mutable holder so the helpers need no ``global`` statement."""

    def __init__(self) -> None:
        self.value: ManagedWriterMode | None = None


_decided = _DecisionSlot()


def decide_managed_writer_mode() -> ManagedWriterMode:
    """The rollout's one mode decision: resolve, record, cache.

    Idempotent per process: a re-entry returns the first decision without
    re-reading the switch (read-once semantics, task #4121 N1).
    """
    decided = _decided.value
    if decided is not None:
        return decided
    mode = effective_managed_writer_mode()
    _decided.value = mode
    print(f"  · managed-writer mode: {mode.describe()}")
    record_managed_writer(mode.state, list(mode.blocked_reasons))
    if mode.state == "blocked":
        _emit_blocked(mode)
    return mode


def managed_writer_mode() -> ManagedWriterMode | None:
    """The decision this process made at the read point, or None before it."""
    return _decided.value


def _emit_blocked(mode: ManagedWriterMode) -> None:
    """Record one blocked decision in the event stream (at most one a rollout)."""
    from shared.audit_events import insert_event_log

    insert_event_log(
        event_type="managed_writer_blocked",
        agent_id=None,
        source="system",
        payload={"reason": ", ".join(mode.blocked_reasons)},
    )


def _reset_for_tests() -> None:
    """Drop the cached decision (a test process runs many rollouts)."""
    _decided.value = None
