"""The coordinator's side of every remote unit: dispatch, instructions, answers and barriers.

- **Preflight and dispatch** go through each unit's ops server while its root
  is still up: the frozen v1 handoff (`release_image_exec`) runs the unit's
  candidate image's `preflight`, then its `submit`, on the unit's own
  `UnitRequest` document.
- **Instructions** are journaled first (one per included unit, reissued only
  when the order changes, so a continuation keeps every digest), then served
  by the listener; **answers** arrive in the listener's queue and only this
  coordinator thread journals them.
- **Barriers** wait for every included unit to answer its current
  instruction by that instruction's journaled deadline. Before the fence a
  failed or silent unit fails the barrier (the coordinator aborts); after it
  the unit is marked `failed` or `unknown` and leaves the operation, and the
  workload policy judges what that costs.

A fleet of one has no remote unit: every call returns at once and nothing
binds. Remote units cannot be admitted before slice dbgen-8
(`inventory.require_topology`), so outside tests this side waits for it.
"""

from __future__ import annotations

import contextlib
import queue
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

from cli.release_fleet.listener import CoordinatorListener
from cli.release_fleet.policy import UnitKey
from cli.release_fleet.progress import (
    FleetProgress,
    Inclusion,
    Instruction,
    InstructionAction,
    Outcome,
    Report,
    UnitStatus,
)
from cli.release_fleet.request import FleetRequest, UnitSpec
from cli.release_fleet.workload import UnitReport
from cli.release_transition.journal import Journal
from shared.api_contracts.release_handoff import ReleaseImageEntry

Clock = Callable[[], datetime]
# How long one barrier poll waits for the next answer before re-checking deadlines.
_POLL_S = 1.0


class Transport(Protocol):
    """`release_image_exec` of one entry on one unit's ops server."""

    def run(
        self, spec: UnitSpec, entry: ReleaseImageEntry, request: bytes
    ) -> dict[str, object]: ...


class OpsTransport:
    """The unit's ops server, dialled at the URL its unit row advertises."""

    def run(self, spec: UnitSpec, entry: ReleaseImageEntry, request: bytes) -> dict[str, object]:
        import asyncio
        import base64

        from ops.cluster_rpc import dispatch_to_machine
        from ops.ops_cluster import RELEASE_ENTRY_TIMEOUT_S
        from shared.api_contracts.release_handoff import (
            ReleaseImageExecPayload,
            ReleaseImageExecResult,
            ReleaseImageRef,
        )

        payload = ReleaseImageExecPayload(
            entry=entry,
            image=ReleaseImageRef.model_validate(spec.candidate.model_dump()),
            request=base64.b64encode(request).decode(),
        )
        answer = asyncio.run(
            dispatch_to_machine(
                spec.unit.machine,
                "release_image_exec",
                payload.model_dump(mode="json"),
                timeout_s=RELEASE_ENTRY_TIMEOUT_S + 30,
                ops_url=_ops_url(spec.unit),
                retries=0,
            )
        )
        return ReleaseImageExecResult.model_validate(answer).result


def _ops_url(unit: UnitKey) -> str:
    from shared.db import connect

    with connect() as conn:
        row = conn.execute(
            "SELECT url FROM machine_units WHERE machine_name = %s AND home = %s", unit.order
        ).fetchone()
    if row is None or row[0] is None:
        raise RuntimeError(f"unit {unit.label} advertises no ops URL")
    return str(row[0])


def _utc_now() -> datetime:
    return datetime.now(UTC)


class UnitBarrierError(RuntimeError):
    """A unit failed or stayed silent at a barrier before the fence."""


class RemoteUnits:
    def __init__(
        self,
        request: FleetRequest,
        *,
        transport: Transport | None = None,
        listener: CoordinatorListener | None = None,
        clock: Clock = _utc_now,
    ) -> None:
        self.request = request
        self._transport = transport
        self._listener = listener
        self._bound = False
        self.clock = clock

    # ── views ───────────────────────────────────────────────────────────────

    @staticmethod
    def _progress(journal: Journal) -> FleetProgress:
        progress = journal.operation.fleet
        if progress is None:
            raise TypeError("remote units belong to a fleet operation")
        return progress

    def included(self, journal: Journal) -> tuple[UnitStatus, ...]:
        return tuple(s for s in self._progress(journal).units if s.inclusion == "included")

    def _serve(self, journal: Journal) -> CoordinatorListener:
        """The operation's listener, bound on first need and serving the journal's instructions."""
        endpoint = self.request.coordinator
        if endpoint is None:
            raise ValueError("a fleet with remote units names its coordinator endpoint")
        if self._listener is None:
            units = tuple(status.unit for status in self._progress(journal).units)
            self._listener = CoordinatorListener(self.request.id, Path(self.request.home), units)
        if not self._bound:
            self._listener.start(endpoint.host, endpoint.port)
            self._bound = True
        progress = self._progress(journal)
        self._listener.publish(s.instruction for s in progress.units if s.instruction)
        return self._listener

    def _transported(self) -> Transport:
        if self._transport is None:
            self._transport = OpsTransport()
        return self._transport

    # ── preflight and dispatch ──────────────────────────────────────────────

    def preflight(self, journal: Journal) -> None:
        """Every included unit's candidate answers its read-only preflight."""
        for status in self.included(journal):
            answer = self._run(status.unit, "preflight")
            if answer.get("ready") is not True:
                raise UnitBarrierError(f"unit {status.unit.label} is not ready: {answer}")

    def dispatch(self, journal: Journal) -> None:
        """Launch every unit's executor, then wait for each to report `dispatched`."""
        if not self.included(journal):
            return
        self.instruct(journal, "standby", bound_s=self.request.policy.start_s)
        for status in self.included(journal):
            if status.answered is None:
                self._run(status.unit, "submit")
        self.barrier(journal, strict=True)

    def _run(self, unit: UnitKey, entry: ReleaseImageEntry) -> dict[str, object]:
        document = self.request.unit_request(unit).model_dump_json().encode()
        return self._transported().run(self.request.spec(unit), entry, document)

    # ── instructions ────────────────────────────────────────────────────────

    def instruct(
        self,
        journal: Journal,
        action: InstructionAction,
        *,
        bound_s: float,
        generation: int | None = None,
        outcome: Outcome | None = None,
    ) -> None:
        """Journal `action` for every included unit, then serve it.

        A dispatched unit that left the operation (operator-excluded, failed
        or unknown) gets one `excluded` order instead: close wherever it
        stands and stay closed until a converge.
        """
        progress = self._progress(journal)
        deadline = self.clock() + timedelta(seconds=bound_s)
        statuses: list[UnitStatus] = []
        for status in progress.units:
            if status.inclusion == "included" and (
                status.instruction is not None or action == "standby"
            ):
                # Only `standby` reaches a unit that was never dispatched.
                order = self._order(journal, status, action, deadline, generation, outcome)
            elif status.instruction is not None and status.instruction.action != "excluded":
                order = self._order(journal, status, "excluded", deadline, None, None)
            else:
                order = None
            if order is None or (status.instruction and status.instruction.same_order(order)):
                statuses.append(status)
            else:
                statuses.append(status.model_copy(update={"instruction": order, "report": None}))
        if tuple(statuses) != progress.units:
            journal.record_fleet(progress.model_copy(update={"units": tuple(statuses)}))
        if any(status.instruction for status in statuses):
            self._serve(journal)

    def _order(
        self,
        journal: Journal,
        status: UnitStatus,
        action: InstructionAction,
        deadline: datetime,
        generation: int | None,
        outcome: Outcome | None,
    ) -> Instruction:
        operation = journal.operation
        direction = operation.direction or "candidate"
        spec = self.request.spec(status.unit)
        restores = direction == "previous" or action == "restore"
        return Instruction(
            operation=self.request.id,
            unit=status.unit,
            sequence=1 if status.instruction is None else status.instruction.sequence + 1,
            action=action,
            direction=direction,
            image=(spec.previous if restores else spec.candidate).selector,
            maintenance_at=operation.maintenance_at,
            generation=generation,
            deadline=deadline,
            outcome=outcome,
        )

    # ── answers and barriers ────────────────────────────────────────────────

    def _journal_answers(self, journal: Journal, wait_s: float) -> None:
        """Journal every queued answer to a unit's current instruction."""
        listener = self._serve(journal)
        answers: list[Report] = []
        with contextlib.suppress(queue.Empty):  # no (further) answer queued yet
            answers.append(listener.reports.get(timeout=wait_s))
            while True:
                answers.append(listener.reports.get_nowait())
        latest = {answer.unit: answer for answer in answers}
        progress = self._progress(journal)
        statuses = tuple(
            status.model_copy(update={"report": latest[status.unit]})
            if _answers(status, latest.get(status.unit))
            else status
            for status in progress.units
        )
        if statuses != progress.units:
            journal.record_fleet(progress.model_copy(update={"units": statuses}))

    def barrier(self, journal: Journal, *, strict: bool) -> dict[UnitKey, Report]:
        """Wait until every included unit answered its current instruction or passed its deadline.

        Strict (before the fence): a failed or silent unit raises. Otherwise it
        leaves the operation as `failed` or `unknown`, journaled.
        """
        while True:
            waiting = tuple(s for s in self.included(journal) if s.instruction is not None)
            marks = self._marks(waiting)
            if marks and strict:
                names = sorted(unit.label for unit in marks)
                raise UnitBarrierError(f"units failed or did not answer before the fence: {names}")
            if marks:
                self._leave(journal, marks)
                continue
            if all(status.answered is not None for status in waiting):
                return {s.unit: s.report for s in waiting if s.report is not None}
            self._journal_answers(journal, _POLL_S)

    def _marks(self, statuses: tuple[UnitStatus, ...]) -> dict[UnitKey, Inclusion]:
        """Units that answered `failed`, and units silent past their deadline."""
        marks: dict[UnitKey, Inclusion] = {}
        for status in statuses:
            if status.answered == "failed":
                marks[status.unit] = "failed"
            elif self._late(status):
                marks[status.unit] = "unknown"
        return marks

    def _late(self, status: UnitStatus) -> bool:
        deadline = None if status.instruction is None else status.instruction.deadline
        return status.answered is None and deadline is not None and self.clock() >= deadline

    def _leave(self, journal: Journal, marks: dict[UnitKey, Inclusion]) -> None:
        progress = self._progress(journal)
        phase = journal.operation.phase
        statuses = tuple(
            status.model_copy(
                update={
                    "inclusion": marks[status.unit],
                    "reason": f"{marks[status.unit]} at {phase}",
                }
            )
            if status.unit in marks
            else status
            for status in progress.units
        )
        journal.record_fleet(progress.model_copy(update={"units": statuses}))

    def unit_reports(
        self, journal: Journal, *, fresh_from: datetime | None = None, wait_s: float = 0.0
    ) -> tuple[UnitReport, ...]:
        """The workload policy's unit evidence: a ready sample per answering unit
        and a failed mark per failed one; an unknown unit has none (unknown).

        With `fresh_from` (a window's end) it first waits up to `wait_s` for
        every answering unit to report again at or after it; a unit that does
        not is left with its older sample, which the policy judges unknown.
        """
        deadline = self.clock() + timedelta(seconds=wait_s)
        while self.included(journal):
            self._journal_answers(journal, 0.0)
            stale = [
                status
                for status in self.included(journal)
                if fresh_from is not None
                and (status.report is None or status.report.at < fresh_from)
            ]
            if not stale or self.clock() >= deadline:
                break
            self._journal_answers(journal, _POLL_S)
        reports: list[UnitReport] = []
        for status in self._progress(journal).units:
            if status.inclusion == "failed":
                reports.append(
                    UnitReport(
                        unit=status.unit,
                        state="failed",
                        observed_at=self.clock(),
                        detail=status.reason,
                    )
                )
            elif status.inclusion == "included" and status.answered not in {None, "failed"}:
                assert status.report is not None  # noqa: S101 — answered implies a report
                reports.append(
                    UnitReport(unit=status.unit, state="ready", observed_at=status.report.at)
                )
        return tuple(reports)

    def finish(self, journal: Journal, outcome: Outcome) -> None:
        """Tell every unit how the operation completed (the ones that left it:
        to stay closed); wait within the drain bound."""
        self.instruct(journal, "complete", bound_s=self.request.policy.drain_s, outcome=outcome)
        self.barrier(journal, strict=False)

    def close(self) -> None:
        if self._listener is not None and self._bound:
            self._listener.close()
            self._bound = False


def _answers(status: UnitStatus, report: Report | None) -> bool:
    current = status.instruction
    return report is not None and current is not None and report.instruction == current.digest
