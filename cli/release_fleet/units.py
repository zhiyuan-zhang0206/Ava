"""The coordinator's side of every remote unit: instructions, answers and barriers.

A fleet of one has no remote unit, so every barrier here is satisfied at once.
"""

from __future__ import annotations

from datetime import datetime

from cli.release_fleet.inventory import NETWORKED_REFUSAL
from cli.release_fleet.policy import UnitKey
from cli.release_fleet.progress import InstructionAction, Outcome, Report
from cli.release_fleet.request import FleetRequest
from cli.release_fleet.workload import UnitReport
from cli.release_transition.journal import Journal


class RemoteUnits:
    def __init__(self, request: FleetRequest) -> None:
        self.request = request

    def _included(self, journal: Journal) -> tuple[UnitKey, ...]:
        progress = journal.operation.fleet
        if progress is None:
            raise TypeError("remote units belong to a fleet operation")
        included = tuple(s.unit for s in progress.units if s.inclusion == "included")
        if included:
            raise RuntimeError(NETWORKED_REFUSAL)
        return included

    def preflight(self, journal: Journal) -> None:
        self._included(journal)

    def dispatch(self, journal: Journal) -> None:
        self._included(journal)

    def instruct(
        self,
        journal: Journal,
        action: InstructionAction,
        *,
        generation: int | None = None,
        deadline: datetime | None = None,
    ) -> None:
        del action, generation, deadline
        self._included(journal)

    def collect(self, journal: Journal, deadline: datetime) -> dict[UnitKey, Report]:
        del deadline
        self._included(journal)
        return {}

    def collect_start(self, journal: Journal, deadline: datetime) -> tuple[UnitReport, ...]:
        del deadline
        self._included(journal)
        return ()

    def collect_watch(self, journal: Journal) -> tuple[UnitReport, ...]:
        self._included(journal)
        return ()

    def open_issuance(self, journal: Journal) -> None:
        self._included(journal)

    def finish(self, journal: Journal, outcome: Outcome) -> None:
        del outcome
        self._included(journal)

    def close(self) -> None:
        return
