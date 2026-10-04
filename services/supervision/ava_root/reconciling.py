"""Custody reconciliation for one supervisor's units (task #4872, route C).

The pass proves each held record releasable — every recorded birth re-verified
against its captured native identity, the unit's process group read for
members — and clears only those; anything less keeps the record and its
evidence. It never signals. It runs under the supervisor's mutation lock
before a fresh spawn reuses a unit's record slot, and once per health round,
where a release also drops the retained dead generation so the monitor can
revive the unit again.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, MutableMapping
from pathlib import Path

from services.supervision.ava_root import custody
from services.supervision.ava_root.manifest import UnitState
from services.supervision.ava_root.unit_records import _UnitRuntime

_log = logging.getLogger("services.supervision.ava_root.supervisor")
"""The supervisor's channel, reused: these lines are its reconcile operations."""


class ReconcilingMixin:
    """The supervisor's custody reconcile family.

    A pass examines every record no live generation owns; a record an active
    generation owns is that generation's own bookkeeping, not stale evidence,
    and stays untouched and silent. A proven-gone record is cleared, and the
    runtime's retained dead generation it explained is dropped with it.
    """

    _run_dir: Path
    _lock: asyncio.Lock
    _units: dict[str, _UnitRuntime]
    _is_active: Callable[[_UnitRuntime], bool]
    _reconcile_reports: dict[str, str]
    """Last-reported retained outcome per unit, so a repeated pass reports a
    repeat once (task #4872, C-1); releases always report."""

    async def reconcile_custody(self) -> list[custody.ReconcileOutcome]:
        """One reconcile pass, under the mutation lock.

        The repeated caller is change-driven: a retained record reports on
        first sight and on every evidence change, never once per round; a
        release always reports (task #4872, C-1).
        """
        async with self._lock:
            outcomes: list[custody.ReconcileOutcome] = []
            for unit in self._reconcile_candidates():
                outcome = self._reconcile_one(unit, reports=self._reconcile_reports)
                if outcome is not None:
                    outcomes.append(outcome)
            return outcomes

    def _reconcile_candidates(self) -> list[str]:
        """Record units no active generation currently owns, in name order."""
        candidates: list[str] = []
        for path in custody.record_paths(self._run_dir):
            runtime = self._units.get(path.stem)
            if runtime is not None and self._is_active(runtime):
                continue
            candidates.append(path.stem)
        return candidates

    def _reconcile_one(
        self, unit: str, *, reports: MutableMapping[str, str] | None = None
    ) -> custody.ReconcileOutcome | None:
        """Reconcile one unit's record; drop its retained dead generation on release.

        `reports` (a repeated pass) dedupes unchanged retained repeats; it is
        omitted for one-shot examinations, which report every record (C-1).
        """
        outcome = custody.reconcile_record(self._run_dir, unit, reports=reports)
        if outcome is None or outcome.decision != "released":
            return outcome
        runtime = self._units.get(unit)
        if runtime is None or runtime.generation is None or self._is_active(runtime):
            return outcome
        runtime.generation = None
        runtime.state = UnitState.STOPPED
        runtime.last_error = None
        _log.warning(
            "unit %s: custody released by reconcile (%s) — the retained generation is dropped",
            unit,
            outcome.evidence,
        )
        return outcome

    def _new_custody(self, unit: str) -> custody.ServiceCustody:
        """Open a unit's record slot; a stale record reconciles once, then retries (C-3)."""
        try:
            return custody.ServiceCustody(self._run_dir, unit)
        except FileExistsError as exc:
            outcome = self._reconcile_one(unit, reports=self._reconcile_reports)
            if outcome is not None and outcome.decision != "released":
                record = self._run_dir / "custody" / f"{unit}.json"
                raise FileExistsError(
                    f"unit {unit}: retained custody blocks the spawn — {outcome.evidence} "
                    f"(record: {record})"
                ) from exc
            return custody.ServiceCustody(self._run_dir, unit)
