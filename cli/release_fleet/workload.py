"""Workload verdicts: the `starting_units` barrier and the post-resume watch window.

Pure and deterministic: the verdict is a function of the captured policy, the
frozen cohort, the observed evidence and the caller's `now`. Missing evidence
is unknown, and unknown is never healthy: a unit without a fresh ready report,
an agent without a fresh observation, or a shared-core signal without a fresh
ok sample counts against the release. The coordinator journals the verdict it
acts on, then executes it; nothing here delivers, selects or publishes.

The evidence is the coordinator's own observation of the operation. The legacy
health probe, heartbeat liveness pass and their alert rows are not evidence.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta
from typing import Literal, NamedTuple, Self, get_args

from pydantic import AwareDatetime, Field, model_validator

from cli.release_fleet.policy import Cohort, FleetPolicy, UnitKey
from cli.release_transition.request import Record
from shared.cluster.authority.model import Direction

CoreSignal = Literal[
    "gateway",  # the gateway unit's selected services are ready
    "database",
    "pooler",
    "redis",
    "schedules",  # schedule manager
    "delivery",
    "fence",  # the revoked generation stays revoked
    "authorization",  # the minted generation is the active one
]
CORE_SIGNALS: tuple[CoreSignal, ...] = get_args(CoreSignal)
AffectedReason = Literal[
    "unit_failed", "unit_unknown", "runtime_error", "quarantined", "not_live", "unobserved"
]
_REASON_ORDER: tuple[AffectedReason, ...] = get_args(AffectedReason)
Stage = Literal["start", "watch"]
# A unit's judged state; "pending" is not judged until the interval closes.
UnitState = Literal["ready", "failed", "unknown", "pending"]
Action = Literal["proceed", "watch", "commit", "recover", "hold"]
Outcome = Literal["clean", "degraded"]
_ACTIONS: dict[Stage, frozenset[Action]] = {
    "start": frozenset({"proceed", "recover", "hold"}),
    "watch": frozenset({"watch", "commit", "recover", "hold"}),
}


class UnitReport(Record):
    """A unit's state. `failed` is the coordinator's recorded mark and stays
    failed; `ready` is a sample that vouches only for its own time."""

    unit: UnitKey
    state: Literal["ready", "failed"]
    observed_at: AwareDatetime
    detail: str | None = Field(default=None, max_length=512)


class AgentReport(Record):
    """One sample of a cohort agent.

    `live`: a live incarnation of the agent on its cohort unit at `observed_at`.
    `runtime_error`: a turn ended with a runtime-class error since the interval
    began. `quarantined`: the agent is quarantined as outcome-unknown.
    """

    agent: int = Field(ge=1)
    live: bool
    runtime_error: bool
    quarantined: bool
    observed_at: AwareDatetime


class CoreReport(Record):
    signal: CoreSignal
    ok: bool
    observed_at: AwareDatetime
    detail: str | None = Field(default=None, max_length=512)


class Evidence(Record):
    """Samples the coordinator gathered for one judged interval `[since, now]`.

    `since` is when the gateway started (start barrier) or when admission
    resumed (watch). Samples older than `since` are not evidence of the
    interval and are ignored, except a unit's recorded `failed` mark.
    """

    since: AwareDatetime
    units: tuple[UnitReport, ...] = ()
    agents: tuple[AgentReport, ...] = ()
    core: tuple[CoreReport, ...] = ()

    def of_agent(self, agent: int) -> list[AgentReport]:
        """This agent's samples within the interval."""
        return [s for s in self.agents if s.agent == agent and s.observed_at >= self.since]

    def of_signal(self, signal: CoreSignal) -> list[CoreReport]:
        """This core signal's samples within the interval."""
        return [s for s in self.core if s.signal == signal and s.observed_at >= self.since]


class AffectedAgent(Record):
    agent: int = Field(ge=1)
    unit: UnitKey
    reasons: tuple[AffectedReason, ...] = Field(min_length=1)


class CoreFailure(Record):
    signal: CoreSignal
    state: Literal["failed", "unknown"]
    detail: str | None = Field(default=None, max_length=512)


class Verdict(Record):
    """The decision the coordinator journals, then executes.

    A `watch` verdict lists only what is already definitive (recorded unit
    failures, runtime errors, quarantines, failed core samples); unknowns are
    judged when the window closes.
    """

    stage: Stage
    direction: Direction
    action: Action
    decided_at: AwareDatetime
    window_end: AwareDatetime | None = None
    outcome: Outcome | None = None
    cohort_size: int = Field(ge=0)
    affected: tuple[AffectedAgent, ...] = ()
    failed_units: tuple[UnitKey, ...] = ()
    unknown_units: tuple[UnitKey, ...] = ()
    core: tuple[CoreFailure, ...] = ()
    threshold_exceeded: bool = False

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if self.action not in _ACTIONS[self.stage]:
            raise ValueError(f"a {self.stage} verdict cannot {self.action}")
        if (self.window_end is not None) != (self.stage == "watch"):
            raise ValueError("only a watch verdict carries its window end")
        if (self.outcome is not None) != (self.action == "commit"):
            raise ValueError("only a commit carries an outcome")
        if self.action == "recover" and self.direction == "previous":
            raise ValueError("recovery happens at most once; the previous direction holds")
        failing = bool(self.core) or self.threshold_exceeded
        if failing != (self.action in {"recover", "hold"}):
            raise ValueError("recover and hold follow exactly a core failure or the threshold")
        if len(self.affected) > self.cohort_size:
            raise ValueError("affected agents must be a subset of the cohort")
        return self

    @property
    def exercised(self) -> bool:
        """A release with no live workload proved nothing about workload."""
        return self.cohort_size > 0


class _Assessment(NamedTuple):
    affected: tuple[AffectedAgent, ...]
    failed_units: tuple[UnitKey, ...]
    unknown_units: tuple[UnitKey, ...]
    core: tuple[CoreFailure, ...]
    threshold_exceeded: bool


def first_sightings(
    known: tuple[AgentReport, ...], sampled: tuple[AgentReport, ...], *, since: datetime
) -> tuple[AgentReport, ...]:
    """The samples that show an agent's runtime error or quarantine not yet known.

    Such a fact is definitive — it can only grow within the interval — but
    the signal it is read from is not: a completed turn clears the fatal-turn
    mark. The coordinator journals each first sighting since `since` and
    judges it with every later sample, so a window never forgets it.
    """
    seen = {
        (report.agent, fact)
        for report in known
        if report.observed_at >= since
        for fact in _facts(report)
    }
    sightings: list[AgentReport] = []
    for sample in sampled:
        facts = {(sample.agent, fact) for fact in _facts(sample)}
        if facts - seen:
            sightings.append(sample)
            seen |= facts
    return tuple(sightings)


def _facts(sample: AgentReport) -> set[AffectedReason]:
    shown: set[AffectedReason] = set()
    if sample.runtime_error:
        shown.add("runtime_error")
    if sample.quarantined:
        shown.add("quarantined")
    return shown


def judge_start(
    policy: FleetPolicy,
    cohort: Cohort,
    evidence: Evidence,
    *,
    direction: Direction,
    now: datetime,
) -> Verdict:
    """The `starting_units` barrier, once every unit is ready or past its deadline.

    Recover on a shared-core failure or when failed and unknown units carry
    more than the threshold of the cohort; otherwise proceed, with those units
    marked and alerted. Agents have not resumed yet, so they are judged only
    through their unit, and agent samples are refused here.
    """
    found = _assess(
        policy, cohort, evidence, now=now, stage="start", fresh_from=evidence.since, final=True
    )
    action = _decide("proceed", found, direction)
    return _verdict("start", direction, action, cohort, found, now=now, window_end=None)


def judge_watch(
    policy: FleetPolicy,
    cohort: Cohort,
    evidence: Evidence,
    *,
    direction: Direction,
    now: datetime,
) -> Verdict:
    """The watch window `[since, since + watch_s]` after admission resumed.

    Before the window ends, only definitive facts count: they can only grow,
    so recovering as soon as they fail the policy is the end decision made
    early. At the end every unit, agent and core signal needs a sample taken at
    or after the window end; anything unsampled counts against the release.
    """
    end = evidence.since + timedelta(seconds=policy.watch_s)
    final = now >= end
    found = _assess(policy, cohort, evidence, now=now, stage="watch", fresh_from=end, final=final)
    action = _decide("commit" if final else "watch", found, direction)
    return _verdict("watch", direction, action, cohort, found, now=now, window_end=end)


def _decide(passing: Action, found: _Assessment, direction: Direction) -> Action:
    """A failing verdict recovers once; the recovery direction itself holds."""
    if found.core or found.threshold_exceeded:
        return "recover" if direction == "candidate" else "hold"
    return passing


def _verdict(
    stage: Stage,
    direction: Direction,
    action: Action,
    cohort: Cohort,
    found: _Assessment,
    *,
    now: datetime,
    window_end: datetime | None,
) -> Verdict:
    outcome: Outcome | None = None
    if action == "commit":
        degraded = found.affected or found.failed_units or found.unknown_units
        outcome = "degraded" if degraded else "clean"
    return Verdict(
        stage=stage,
        direction=direction,
        action=action,
        decided_at=now,
        window_end=window_end,
        outcome=outcome,
        cohort_size=cohort.size,
        affected=found.affected,
        failed_units=found.failed_units,
        unknown_units=found.unknown_units,
        core=found.core,
        threshold_exceeded=found.threshold_exceeded,
    )


def _assess(
    policy: FleetPolicy,
    cohort: Cohort,
    evidence: Evidence,
    *,
    now: datetime,
    stage: Stage,
    fresh_from: datetime,
    final: bool,
) -> _Assessment:
    _require_coherent(cohort, evidence, now)
    if stage == "start" and evidence.agents:
        raise ValueError("agents resume after the start barrier; their samples judge the watch")
    units = _unit_states(cohort, evidence, fresh_from=fresh_from, final=final)
    affected = _affected(cohort, units, evidence, stage=stage, fresh_from=fresh_from, final=final)
    return _Assessment(
        affected=affected,
        failed_units=tuple(unit for unit, state in units.items() if state == "failed"),
        unknown_units=tuple(unit for unit, state in units.items() if state == "unknown"),
        core=_core_failures(evidence, units[cohort.gateway], fresh_from=fresh_from, final=final),
        threshold_exceeded=policy.exceeds(len(affected), cohort.size),
    )


def _require_coherent(cohort: Cohort, evidence: Evidence, now: datetime) -> None:
    """Inconsistent evidence is a coordinator defect, never a health signal.

    After this check every sample lies at or before `now`.
    """
    if now < evidence.since:
        raise ValueError("a verdict cannot precede the interval it judges")
    samples: Iterable[UnitReport | AgentReport | CoreReport] = (
        *evidence.units,
        *evidence.agents,
        *evidence.core,
    )
    if any(sample.observed_at > now for sample in samples):
        raise ValueError("evidence cannot be newer than the verdict")
    if outside := {report.unit.label for report in evidence.units} - {
        unit.label for unit in cohort.unit_keys
    }:
        raise ValueError(f"evidence names units outside the operation: {sorted(outside)}")
    if strangers := {report.agent for report in evidence.agents} - cohort.members.keys():
        raise ValueError(f"evidence names agents outside the frozen cohort: {sorted(strangers)}")


def _unit_states(
    cohort: Cohort, evidence: Evidence, *, fresh_from: datetime, final: bool
) -> dict[UnitKey, UnitState]:
    states: dict[UnitKey, UnitState] = {}
    for unit in cohort.unit_keys:
        reports = [report for report in evidence.units if report.unit == unit]
        if any(report.state == "failed" for report in reports):
            states[unit] = "failed"
        elif not final:
            states[unit] = "pending"
        elif any(report.observed_at >= fresh_from for report in reports):
            states[unit] = "ready"
        else:
            states[unit] = "unknown"
    return states


def _affected(
    cohort: Cohort,
    units: dict[UnitKey, UnitState],
    evidence: Evidence,
    *,
    stage: Stage,
    fresh_from: datetime,
    final: bool,
) -> tuple[AffectedAgent, ...]:
    affected: list[AffectedAgent] = []
    for agent, unit in sorted(cohort.members.items()):
        reasons = _unit_reasons(units[unit])
        if stage == "watch":
            reasons |= _sample_reasons(evidence.of_agent(agent), fresh_from=fresh_from, final=final)
        if reasons:
            affected.append(AffectedAgent(agent=agent, unit=unit, reasons=_ordered(reasons)))
    return tuple(affected)


def _unit_reasons(state: UnitState) -> set[AffectedReason]:
    if state == "failed":
        return {"unit_failed"}
    if state == "unknown":
        return {"unit_unknown"}
    return set()


def _ordered(reasons: set[AffectedReason]) -> tuple[AffectedReason, ...]:
    return tuple(reason for reason in _REASON_ORDER if reason in reasons)


def _sample_reasons(
    samples: list[AgentReport], *, fresh_from: datetime, final: bool
) -> set[AffectedReason]:
    """Errors and quarantines anywhere in the interval; liveness only at its close."""
    reasons: set[AffectedReason] = set()
    for sample in samples:
        reasons |= _facts(sample)
    if final and (liveness := _liveness(samples, fresh_from)) is not None:
        reasons.add(liveness)
    return reasons


def _liveness(samples: list[AgentReport], fresh_from: datetime) -> AffectedReason | None:
    fresh = [sample for sample in samples if sample.observed_at >= fresh_from]
    if not fresh:
        return "unobserved"
    return None if all(sample.live for sample in fresh) else "not_live"


def _core_failures(
    evidence: Evidence, gateway_unit: UnitState, *, fresh_from: datetime, final: bool
) -> tuple[CoreFailure, ...]:
    """Every signal needs a fresh ok sample and no failed one in the interval.

    The gateway unit's own failed or unknown state is a gateway failure
    whatever the gateway samples say.
    """
    failures: list[CoreFailure] = []
    for signal in CORE_SIGNALS:
        if signal == "gateway" and gateway_unit in {"failed", "unknown"}:
            state: Literal["failed", "unknown"] = (
                "failed" if gateway_unit == "failed" else "unknown"
            )
            failures.append(CoreFailure(signal=signal, state=state, detail=f"gateway unit {state}"))
        elif failure := _core_signal(
            signal, evidence.of_signal(signal), fresh_from=fresh_from, final=final
        ):
            failures.append(failure)
    return tuple(failures)


def _core_signal(
    signal: CoreSignal, samples: list[CoreReport], *, fresh_from: datetime, final: bool
) -> CoreFailure | None:
    if failed := [sample for sample in samples if not sample.ok]:
        return CoreFailure(signal=signal, state="failed", detail=failed[0].detail)
    if final and not any(sample.observed_at >= fresh_from for sample in samples):
        return CoreFailure(signal=signal, state="unknown")
    return None
