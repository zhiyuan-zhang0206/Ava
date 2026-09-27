"""A remote unit's executor: it follows the coordinator's instructions and chooses nothing.

The unit journal is the unit home's `Operation` (kind `unit`). Every pulled
instruction is journaled as acted on (its digest) before its effect, and the
answer is journaled before it is sent, so after either side dies the two
views reconcile by instruction digest: an instruction already answered is
answered again with the journaled report, one acted on but unanswered is
reconciled by running its phase's effect again (each effect reconciles real
state, as the one-home transition's do), and an older instruction is ignored.

A `restore` is the coordinator's abort (the unchanged previous root on the
unchanged generation); an instruction naming the `previous` direction is its
one recovery, followed from the unit's current phase. A unit that loses the
coordinator holds its phase and keeps polling up to its lifetime; it never
guesses the next phase.

The capability exchange at `start` (write generation n+1 over the channel)
is slice dbgen-8: `DeferredExchange` refuses it by name, and a remote unit
cannot be submitted until then (`inventory.require_topology`).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

from cli.release_fleet.client import (
    CapabilityDeferredError,
    CoordinatorAwayError,
    CoordinatorClient,
    StaleReportError,
)
from cli.release_fleet.policy import UnitCohort, drain_report
from cli.release_fleet.progress import Instruction, Report, ReportState, UnitProgress, next_phase
from cli.release_fleet.request import UnitRequest
from cli.release_transition.journal import Journal, Operation
from shared.log import logger
from shared.maintenance_state import MaintenanceHold

Clock = Callable[[], datetime]
POLL_S = 5.0
# A unit executor that has not heard from its coordinator this long holds.
LIFETIME_S = 24 * 3600.0
# Each action's phases, in order; the effect of a phase runs while the journal is at it.
_PHASES: dict[str, tuple[str, ...]] = {
    "quiesce": ("quiescing",),
    "close": ("stopping",),
    "start": ("authorizing", "selecting", "starting", "observing"),
    "resume": ("resuming",),
    "restore": ("restoring",),
}
_ANSWER: dict[str, ReportState] = {
    "standby": "dispatched",
    "quiesce": "drained",
    "close": "closed",
    "excluded": "closed",
    "wait": "closed",
    "start": "ready",
    "resume": "resumed",
    "watch": "resumed",
    "restore": "restored",
    "complete": "completed",
}


class UnitEffects(Protocol):
    """One home's release effects (`LocalTransition` for a real unit)."""

    def quiesce(self, operation: Operation) -> MaintenanceHold: ...
    def stop(self, operation: Operation) -> None: ...
    def select(self, operation: Operation) -> None: ...
    def start(self, journal: Journal) -> None: ...
    def observe(self, operation: Operation) -> None: ...
    def resume(self, operation: Operation) -> None: ...
    def restore(self, journal: Journal) -> None: ...


class CapabilityExchange(Protocol):
    """The unit's write-generation capability (slice dbgen-8 implements the exchange)."""

    def installed(self, home: Path) -> int | None:
        """The installed capability's generation number, or None."""
        ...

    def exchange(self, journal: Journal, instruction: Instruction) -> None:
        """Install `instruction.generation`'s capability before this unit selects."""
        ...


class DeferredExchange:
    """No exchange yet: a start on a new generation refuses, naming dbgen-8."""

    def __init__(self, client: CoordinatorClient) -> None:
        self._client = client

    def installed(self, home: Path) -> int | None:
        from shared.cluster.authority.unit import load_unit_capability

        capability = load_unit_capability(home)
        return None if capability is None else capability.generation.number

    def exchange(self, journal: Journal, instruction: Instruction) -> None:
        del journal, instruction
        self._client.capability()  # raises CapabilityDeferredError until dbgen-8


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _detail(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"[:2048]


class Follower:
    def __init__(
        self,
        journal: Journal,
        effects: UnitEffects,
        client: CoordinatorClient,
        exchange: CapabilityExchange,
        *,
        clock: Clock = _utc_now,
        sleep: Callable[[float], None] = time.sleep,
        poll_s: float = POLL_S,
        lifetime_s: float = LIFETIME_S,
    ) -> None:
        self.journal = journal
        self.effects = effects
        self.client = client
        self.exchange = exchange
        self.clock = clock
        self.sleep = sleep
        self.poll_s = poll_s
        self.lifetime_s = lifetime_s

    @property
    def request(self) -> UnitRequest:
        request = self.journal.operation.request
        if not isinstance(request, UnitRequest):
            raise TypeError("a follower drives only a unit operation")
        return request

    @property
    def progress(self) -> UnitProgress:
        progress = self.journal.operation.unit
        if progress is None:
            raise TypeError("a unit operation carries unit progress")
        return progress

    def run(self) -> None:
        """Pull, act, answer, until the coordinator completes this unit's operation."""
        heard = self.clock()
        if self.progress.admitted is None:
            admitted = self.exchange.installed(Path(self.request.home))
            self.journal.record_fleet(self.progress.model_copy(update={"admitted": admitted}))
        while not self.journal.operation.terminal:
            try:
                instruction = self.client.instruction()
            except CoordinatorAwayError as away:
                logger.info("[release-fleet] coordinator away: {error}", error=away)
                instruction = None
            if instruction is not None:
                heard = self.clock()
                self.follow(instruction)
                if instruction.action == "excluded":
                    return  # closed and held; only a converge operation brings it back
            elif self.clock() - heard > timedelta(seconds=self.lifetime_s):
                raise RuntimeError("the coordinator was silent past this unit executor's lifetime")
            if not self.journal.operation.terminal:
                self.sleep(self.poll_s)

    # ── one instruction ─────────────────────────────────────────────────────

    def follow(self, instruction: Instruction) -> None:
        self._require_mine(instruction)
        current = self.progress.instruction
        if current is not None and instruction.sequence < current.sequence:
            return  # a replaced instruction; the current one is answered already
        if instruction.digest not in self.progress.acted:
            self._decide(instruction)
            acted = (*self.progress.acted, instruction.digest)
            self.journal.record_fleet(
                self.progress.model_copy(
                    update={"instruction": instruction, "acted": acted, "report": None}
                )
            )
        report = self.progress.report
        if report is None:
            report = self._act(instruction)
            if not self.journal.operation.terminal:
                self.journal.record_fleet(self.progress.model_copy(update={"report": report}))
        elif instruction.action == "watch":
            report = report.model_copy(update={"at": self.clock()})  # a fresh liveness sample
        try:
            self.client.report(report)
        except StaleReportError:
            return  # the coordinator moved on; the next pull brings its instruction
        except CoordinatorAwayError as away:
            logger.info("[release-fleet] report not delivered yet: {error}", error=away)

    def _require_mine(self, instruction: Instruction) -> None:
        request = self.request
        target = request.previous if instruction.direction == "previous" else request.candidate
        if instruction.action == "restore":
            target = request.previous
        if (instruction.operation, instruction.unit) != (request.id, request.unit):
            raise ValueError("an instruction for another operation or unit is refused")
        if instruction.image != target.selector:
            raise ValueError(
                "the instruction names another image than this unit's for its direction"
            )

    def _decide(self, instruction: Instruction) -> None:
        """The coordinator's abort or recovery becomes this unit's own, once."""
        operation = self.journal.operation
        if instruction.action == "excluded":
            return  # it closes wherever it stands, deciding nothing
        if operation.direction == "candidate" and instruction.action == "restore":
            self.journal.abort("the coordinator aborted before the fence", at=self.clock())
        elif operation.direction == "candidate" and instruction.direction == "previous":
            renewed = instruction.maintenance_at != self.progress.maintenance_at
            self.journal.recover(
                "the coordinator recovers to the previous release",
                at=self.clock(),
                maintenance_at=instruction.maintenance_at if renewed else None,
            )
        elif instruction.maintenance_at != self.progress.maintenance_at:
            raise ValueError("the instruction names another maintenance hold than this unit's")

    def _act(self, instruction: Instruction) -> Report:
        """Run the instruction's phases, then answer; a failed effect answers `failed`."""
        try:
            cohort = self._run(instruction)
        except (OSError, ValueError, RuntimeError, CapabilityDeferredError) as exc:
            self.journal.fail(_detail(exc))
            return self._report(instruction, "failed", detail=_detail(exc))
        return self._report(instruction, _ANSWER[instruction.action], cohort=cohort)

    def _run(self, instruction: Instruction) -> UnitCohort | None:
        if instruction.action == "excluded":
            # Close wherever this unit stands and hold; its journal stays
            # incomplete until a converge operation supersedes it.
            self.effects.stop(self.journal.operation)
            return None
        cohort = None
        for phase in _PHASES.get(instruction.action, ()):
            hold = self._through(phase, instruction)
            if hold is not None:
                cohort = drain_report(self.request.unit, hold)
        if instruction.action == "complete":
            if instruction.outcome is None:
                raise ValueError("a complete instruction names its outcome")
            self.journal.complete(instruction.outcome)
        return cohort

    def _report(self, instruction: Instruction, state: ReportState, **fields: object) -> Report:
        return Report.model_validate(
            {
                "operation": instruction.operation,
                "unit": instruction.unit,
                "instruction": instruction.digest,
                "state": state,
                "at": self.clock(),
            }
            | fields
        )

    def _through(self, phase: str, instruction: Instruction) -> MaintenanceHold | None:
        """Advance up to `phase` (never past it), then run its effect."""
        operation = self.journal.operation
        direction = operation.direction or "candidate"
        order = [operation.phase]
        while order[-1] != phase:
            after = next_phase("unit", order[-1], direction)
            if after is None:
                return None  # already past `phase`: its effect is done
            order.append(after)
        for step in order[1:]:
            self.journal.advance(step)  # type: ignore[arg-type]  # a unit phase from the order
        return self._effect(phase, instruction)

    def _effect(self, phase: str, instruction: Instruction) -> MaintenanceHold | None:
        operation = self.journal.operation
        if phase == "quiescing":
            return self.effects.quiesce(operation)
        if phase == "stopping":
            self.effects.stop(operation)
        elif phase == "authorizing":
            self.exchange.exchange(self.journal, instruction)
        elif phase == "selecting":
            self.effects.select(operation)
        elif phase == "starting":
            self.effects.start(self.journal)
        elif phase == "observing":
            self.effects.observe(operation)
        elif phase == "resuming":
            self.effects.resume(operation)
        elif phase == "restoring":
            self.effects.restore(self.journal)
        return None


def run_follower(journal: Journal) -> None:
    """The finite executor's entry for a remote unit's operation."""
    from cli.release_transition.local import LocalTransition
    from shared.cluster.authority.unit import load_unit_enrollment

    request = journal.operation.request
    if not isinstance(request, UnitRequest):
        raise TypeError("a follower drives only a unit operation")
    enrollment = load_unit_enrollment(Path(request.home))
    if enrollment is None:
        raise RuntimeError("this unit holds no enrollment; the coordinator cannot authenticate it")
    client = CoordinatorClient(request.coordinator, request.id, request.unit, enrollment)
    Follower(journal, LocalTransition(request), client, DeferredExchange(client)).run()
