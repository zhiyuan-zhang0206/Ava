"""The fleet coordinator: the gateway host's finite executor decides; units follow.

`Coordinator.run` drives the gateway home's fleet operation (the journal's
`fleet` kind) through its phases. The gateway unit's own effects are
`GatewayUnit`'s (`gateway.py`); remote units receive instructions and answer
through `RemoteUnits` (`units.py`). Every decision is journaled before it acts:
a cohort before its alerts, a verdict before its recovery or commit, a
completion before the operation completes. A continuation after executor death
acts on the journaled decision instead of re-deciding; an operator continuing a
held operation re-runs the held step, so a held start barrier is judged again.

- A failure before the fence **aborts**: every unit restores its unchanged
  previous image on the unchanged generation (`restoring`), outcome `aborted`.
- A candidate failure after the fence **recovers once**: the candidate is fenced
  and the predecessor runs on a new generation, outcome `recovered`.
- Anything else **holds**: the error is journaled with a `held` alert and the
  executor exits; the operator continues with the same submit command.

A failure is any `Exception`, whatever its class (`cli.release_transition.failure`).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Literal

from cli.release_fleet.alerting import (
    FleetAlert,
    drain_alerts,
    recovered_alert,
    verdict_alerts,
)
from cli.release_fleet.gateway import GatewayUnit
from cli.release_fleet.policy import Cohort, UnitCohort, capture_cohort, drain_report
from cli.release_fleet.progress import FLEET_ABORTABLE, AlertRecord, FleetProgress, UnitStatus
from cli.release_fleet.publication import Completion
from cli.release_fleet.request import FleetRequest
from cli.release_fleet.units import RemoteUnits
from cli.release_fleet.workload import Evidence, UnitReport, Verdict, judge_start, judge_watch
from cli.release_transition.failure import OperationFailure, failure_detail
from cli.release_transition.journal import Journal, Operation
from shared.log import logger

Clock = Callable[[], datetime]
# Cadence of watch-window samples; the last sample always follows the window end.
_WATCH_SAMPLE_S = 30.0
_RECOVERABLE = frozenset({"starting", "observing", "starting_units"})


def _utc_now() -> datetime:
    return datetime.now(UTC)


class HeldVerdictError(RuntimeError):
    """A journaled `hold` verdict, executed: its alerts were journaled with it."""


def _needs_lease(operation: Operation) -> bool:
    """Every phase from `quiescing` on runs under the cluster deploy lease.

    `dispatching` takes it; an abort decided before any unit was disturbed
    (at `prepared` or `dispatching`) restores nothing and needs none.
    """
    fleet = operation.fleet
    if operation.phase in {"prepared", "dispatching", "complete"} or fleet is None:
        return False
    if operation.phase == "restoring":
        return fleet.decisions[0].phase not in {"prepared", "dispatching"}
    return True


class Coordinator:
    def __init__(
        self,
        journal: Journal,
        gateway: GatewayUnit,
        units: RemoteUnits,
        *,
        clock: Clock = _utc_now,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.journal = journal
        self.gateway = gateway
        self.units = units
        self.clock = clock
        self.sleep = sleep

    @property
    def operation(self) -> Operation:
        return self.journal.operation

    @property
    def request(self) -> FleetRequest:
        request = self.operation.request
        if not isinstance(request, FleetRequest):
            raise TypeError("the coordinator drives only a fleet operation")
        return request

    @property
    def progress(self) -> FleetProgress:
        progress = self.operation.fleet
        if progress is None:
            raise TypeError("a fleet operation carries fleet progress")
        return progress

    # ── journal helpers ─────────────────────────────────────────────────────

    def _record(self, **changes: object) -> None:
        self.journal.record_fleet(self.progress.model_copy(update=changes))

    def _with_alerts(self, alerts: tuple[FleetAlert, ...], **changes: object) -> None:
        """Journal `changes` and every alert not yet journaled, in one write."""
        known = {record.alert.key for record in self.progress.alerts}
        fresh = tuple(AlertRecord(alert=a) for a in alerts if a.key not in known)
        self._record(alerts=(*self.progress.alerts, *fresh), **changes)

    def _decision_alerts(self) -> None:
        """A journaled recovery is always announced, even if the executor died
        between the decision and its alert (the decision is written first)."""
        for decision in self.progress.decisions:
            if decision.kind == "recover":
                summary = f"recovering from {decision.phase}: {decision.reason}"
                self._with_alerts((self._alert("recovering", decision.at, summary),))

    def _deliver(self) -> None:
        """Deliver journaled alerts; each landed delivery is journaled once."""
        if self.operation.terminal:
            return
        self._decision_alerts()
        for index, record in enumerate(self.progress.alerts):
            landed = self.gateway.deliver(record, self.request.policy.alert_route)
            if set(landed) <= set(record.delivered):
                continue
            merged = tuple(sorted({*record.delivered, *landed}))
            alerts = list(self.progress.alerts)
            alerts[index] = record.model_copy(update={"delivered": merged})
            self._record(alerts=tuple(alerts))

    # ── the phase loop ──────────────────────────────────────────────────────

    def run(self) -> None:
        try:
            while not self.operation.terminal:
                operation = self.operation
                try:
                    self._step(operation)
                except OperationFailure as exc:
                    held = not self._failed(operation, exc)
                    self._deliver()
                    if held:
                        raise
                    continue
                self._deliver()
        finally:
            self.units.close()

    def _step(self, operation: Operation) -> None:
        if _needs_lease(operation):
            # Re-armed after an executor restart; a lost lease fails the step.
            self.gateway.lease.hold()
            self.gateway.lease.require()
        handler: Callable[[Operation], None] = getattr(self, f"_{operation.phase}")
        handler(operation)

    def _failed(self, operation: Operation, exc: BaseException) -> bool:
        """Abort before the fence, recover a failing candidate once, else hold."""
        detail, now = failure_detail(exc), self.clock()
        # The journal and the alert keep class and message; the log keeps the traceback.
        logger.opt(exception=exc).warning(
            "[release-fleet] {phase} failed: {detail}", phase=operation.phase, detail=detail
        )
        if operation.direction == "candidate" and operation.phase in FLEET_ABORTABLE:
            self.journal.abort(detail, at=now)
            return True
        if operation.direction == "candidate" and operation.phase in _RECOVERABLE:
            self.journal.recover(detail, at=now)
            self._decision_alerts()
            return True
        if not isinstance(exc, HeldVerdictError):
            summary = f"held at {operation.phase}: {detail}"
            self._with_alerts((self._alert("held", now, summary),))
        self.journal.fail(detail)
        return False

    def _alert(self, kind: Literal["recovering", "held"], at: datetime, summary: str) -> FleetAlert:
        return FleetAlert(operation=self.request.id, kind=kind, at=at, summary=summary)

    # ── phases ──────────────────────────────────────────────────────────────

    def _prepared(self, _operation: Operation) -> None:
        self.gateway.preflight()
        admitted = self.gateway.preflight_authority()
        self.units.preflight(self.journal)
        self._record(admitted=admitted)
        self.journal.advance("dispatching")

    def _dispatching(self, _operation: Operation) -> None:
        self.gateway.lease.hold()
        self.units.dispatch(self.journal)
        self.journal.advance("quiescing")

    def _quiescing(self, operation: Operation) -> None:
        policy = self.request.policy
        self.units.instruct(self.journal, "quiesce", bound_s=policy.drain_s)
        hold = self.gateway.quiesce(operation)
        # Before the fence a failed or silent unit aborts; a recovery goes on without it.
        reports = self.units.barrier(self.journal, strict=operation.direction == "candidate")
        if self.progress.cohort is None:
            gateway = self.request.gateway
            cohorts: list[UnitCohort] = [drain_report(gateway, hold)]
            cohorts += [report.cohort for report in reports.values() if report.cohort is not None]
            cohort = capture_cohort(gateway=gateway, reports=cohorts, captured_at=self.clock())
            self._with_alerts(drain_alerts(self.request.id, cohort), cohort=cohort)
        self.journal.advance("stopping")

    def _stopping(self, operation: Operation) -> None:
        policy = self.request.policy
        bound = policy.close_s + policy.cancel_grace_s + policy.drain_s
        self.units.instruct(self.journal, "close", bound_s=bound)
        self.units.barrier(self.journal, strict=operation.direction == "candidate")
        # The gateway closes last: runners' drains and closure still need its API.
        self.gateway.stop(operation)
        self.journal.advance("fencing")

    def _fencing(self, _operation: Operation) -> None:
        self.units.instruct(self.journal, "wait", bound_s=self.request.policy.start_s)
        self.gateway.fence(self.journal)
        self.journal.advance("selecting")

    def _selecting(self, operation: Operation) -> None:
        self.gateway.select(operation)
        self.journal.advance("authorizing")

    def _authorizing(self, _operation: Operation) -> None:
        # Each unit exchanges this generation's capability over the channel
        # when it starts (slice dbgen-8).
        self.gateway.authorize(self.journal)
        self.journal.advance("starting")

    def _starting(self, _operation: Operation) -> None:
        if self.progress.started_at is None:
            self._record(started_at=self.clock())
        self.gateway.start(self.journal)
        self.journal.advance("observing")

    def _observing(self, operation: Operation) -> None:
        self.gateway.observe(operation)
        self.journal.advance("starting_units")

    def _starting_units(self, operation: Operation) -> None:
        verdict = self._journaled_verdict("start")
        if verdict is None:
            issue = None if operation.direction is None else operation.issue(operation.direction)
            if issue is None:
                raise RuntimeError("units start only on this direction's authorized generation")
            policy = self.request.policy
            self.units.instruct(
                self.journal, "start", bound_s=policy.start_s, generation=issue.number
            )
            self.units.barrier(self.journal, strict=False)
            verdict = self._judge("start", self.units.unit_reports(self.journal))
        self._act(verdict, on_proceed="resuming")

    def _resuming(self, operation: Operation) -> None:
        self.gateway.resume(operation)
        self.units.instruct(self.journal, "resume", bound_s=self.request.policy.drain_s)
        self.units.barrier(self.journal, strict=False)
        if self.progress.resumed_at is None:
            self._record(resumed_at=self.clock())
        if operation.direction == "candidate":
            self.journal.advance("watching")
            return
        now = self.clock()
        self._with_alerts((recovered_alert(self.request.id, now),))
        self._complete(Completion.model_validate(self._completion("recovered", now)))

    def _watching(self, _operation: Operation) -> None:
        verdict = self._journaled_verdict("watch")
        self.units.instruct(self.journal, "watch", bound_s=self.request.policy.watch_s)
        while verdict is None:
            resumed = self.progress.resumed_at
            if resumed is None:
                raise RuntimeError("the watch window has no recorded resume")
            end = resumed + timedelta(seconds=self.request.policy.watch_s)
            # At the window's end every unit must report again (unknown is not healthy).
            closing = self.clock() >= end
            reports = self.units.unit_reports(
                self.journal,
                fresh_from=end if closing else None,
                wait_s=self.request.policy.drain_s if closing else 0.0,
            )
            candidate = self._judge("watch", reports)
            if candidate.action != "watch":
                verdict = candidate
                break
            policy = self.request.policy
            self._with_alerts(verdict_alerts(self.request.id, policy, self._cohort, candidate))
            self._deliver()  # a window lasts minutes; its alerts do not wait for its end
            remaining = (end - self.clock()).total_seconds()
            self.sleep(max(0.0, min(_WATCH_SAMPLE_S, remaining)))
            self.gateway.lease.require()
        self._act(verdict, on_proceed=None)

    def _restoring(self, _operation: Operation) -> None:
        self.gateway.restore(self.journal)
        self.units.instruct(self.journal, "restore", bound_s=self.request.policy.start_s)
        self.units.barrier(self.journal, strict=False)
        now = self.clock()
        self._complete(Completion.model_validate(self._completion("aborted", now)))

    # ── verdicts and completion ─────────────────────────────────────────────

    @property
    def _cohort(self) -> Cohort:
        cohort = self.progress.cohort
        if cohort is None:
            raise RuntimeError("the operation has no frozen cohort")
        return cohort

    def _journaled_verdict(self, stage: Literal["start", "watch"]) -> Verdict | None:
        """The verdict journaled for this stage and direction that is still to execute.

        A continuation after executor death executes it instead of judging
        again, a `hold` included. A hold already executed — the operation
        recorded its error and the executor exited — is the operator's to
        continue: `ava cluster update --prepared` on a held operation asks for
        the held step again, so it is judged afresh (and may hold again, with a
        new alert). That is an explicit operator action, never an automatic
        retry (decisions/2026-09-27-fleet-core-release-choices.md item 2).
        """
        operation = self.operation
        for verdict in reversed(self.progress.verdicts):
            if verdict.stage == stage and verdict.direction == operation.direction:
                executed = verdict.action == "hold" and operation.error is not None
                return None if executed else verdict
        return None

    def _judge(
        self, stage: Literal["start", "watch"], unit_reports: tuple[UnitReport, ...]
    ) -> Verdict:
        """Sample the gateway unit and its cohort agents, then apply the policy."""
        direction = self.operation.direction
        assert direction is not None  # noqa: S101 — a fleet operation has a direction
        since = self.progress.started_at if stage == "start" else self.progress.resumed_at
        if since is None:
            raise RuntimeError(f"the {stage} barrier has no recorded interval start")
        observed = self.clock()
        samples = self.gateway.sample(
            self.operation, self._cohort, since=since, observed=observed, agents=stage == "watch"
        )
        evidence = Evidence(
            since=since,
            units=(samples.gateway, *unit_reports),
            agents=samples.agents,
            core=samples.core,
        )
        now = max(self.clock(), observed)
        judge = judge_start if stage == "start" else judge_watch
        return judge(self.request.policy, self._cohort, evidence, direction=direction, now=now)

    def _act(self, verdict: Verdict, *, on_proceed: Literal["resuming"] | None) -> None:
        """Journal the verdict (with its alerts and unit marks), then execute it."""
        if verdict not in self.progress.verdicts:
            alerts = verdict_alerts(self.request.id, self.request.policy, self._cohort, verdict)
            self._with_alerts(
                alerts,
                verdicts=(*self.progress.verdicts, verdict),
                units=self._marked(verdict),
            )
        if verdict.action in {"proceed", "commit"}:
            if on_proceed is not None:
                self.journal.advance(on_proceed)
                return
            completion = Completion.committed(
                verdict,
                operation=self.request.id,
                previous=self.gateway.release_of(self.request.previous),
                candidate=self.gateway.release_of(self.request.candidate),
                excluded=tuple(entry.unit for entry in self.request.excluded),
            )
            self._complete(completion)
            return
        reason = f"{verdict.stage} verdict {verdict.action}: {len(verdict.affected)} affected"
        if verdict.action == "recover":
            renewed = verdict.decided_at if verdict.stage == "watch" else None
            self.journal.recover(reason, at=verdict.decided_at, maintenance_at=renewed)
            return
        raise HeldVerdictError(f"release held for the operator: {reason}")

    def _marked(self, verdict: Verdict) -> tuple[UnitStatus, ...]:
        """Failed and unknown units leave the operation; their agents stay affected."""
        marks = dict.fromkeys(verdict.failed_units, "failed")
        marks |= dict.fromkeys(verdict.unknown_units, "unknown")
        statuses: list[UnitStatus] = []
        for status in self.progress.units:
            state = marks.get(status.unit)
            if state is None or status.inclusion != "included":
                statuses.append(status)
                continue
            reason = f"{verdict.stage} barrier: {state}"
            statuses.append(status.model_copy(update={"inclusion": state, "reason": reason}))
        return tuple(statuses)

    def _completion(
        self, outcome: Literal["aborted", "recovered"], at: datetime
    ) -> dict[str, object]:
        stale = sorted(
            (s.unit for s in self.progress.units if s.inclusion != "included"),
            key=lambda unit: unit.order,
        )
        return {
            "operation": self.request.id,
            "at": at,
            "outcome": outcome,
            "previous": self.gateway.release_of(self.request.previous),
            "candidate": self.gateway.release_of(self.request.candidate),
            "stale_units": tuple(stale),
        }

    def _complete(self, completion: Completion) -> None:
        """Publish, tell every unit, deliver, then complete; each step is idempotent."""
        self.gateway.publish(completion)
        self.units.finish(self.journal, completion.outcome)
        self._deliver()
        self.journal.complete(completion.outcome)
        self.gateway.lease.release()


def run_coordinator(journal: Journal) -> None:
    """The finite executor's entry for a fleet operation on the gateway home."""
    request = journal.operation.request
    if not isinstance(request, FleetRequest):
        raise TypeError("the coordinator drives only a fleet operation")
    units = RemoteUnits(request)
    Coordinator(journal, GatewayUnit(request), units).run()
