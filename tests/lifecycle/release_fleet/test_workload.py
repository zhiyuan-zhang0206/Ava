"""The workload decision table: start barrier and watch window.

Unknown is never healthy: a missing unit report, agent observation or core
sample counts against the release; thresholds are strictly above.
"""

from __future__ import annotations

from datetime import datetime

import pytest
from pydantic import ValidationError

from cli.release_fleet.policy import FleetPolicy
from cli.release_fleet.workload import (
    AgentReport,
    CoreFailure,
    CoreReport,
    Evidence,
    UnitReport,
    Verdict,
    first_sightings,
    judge_start,
    judge_watch,
)
from tests.lifecycle.release_fleet.conftest import (
    GATEWAY,
    POLICY,
    RESUMED,
    RUNNER,
    WATCH_END,
    agents_live,
    at,
    core_ok,
    healthy,
    two_units,
    units_ready,
)

TEN = two_units(gateway_agents=(1, 2, 3, 4, 5), runner_agents=(6, 7, 8, 9, 10))
ALL = tuple(range(1, 11))
MID = at(60)


def _errors(agents: tuple[int, ...], when: datetime = MID) -> tuple[AgentReport, ...]:
    return tuple(
        AgentReport(agent=a, live=True, runtime_error=True, quarantined=False, observed_at=when)
        for a in agents
    )


def _start_evidence(*units: UnitReport, core: tuple[CoreReport, ...] | None = None) -> Evidence:
    return Evidence(since=RESUMED, units=units, core=core_ok(at(30)) if core is None else core)


def _without(reports: tuple[CoreReport, ...], signal: str) -> tuple[CoreReport, ...]:
    return tuple(report for report in reports if report.signal != signal)


# --- start barrier -------------------------------------------------------------------


def test_start_proceeds_when_every_unit_is_ready_and_the_core_is_ok() -> None:
    verdict = judge_start(
        POLICY, TEN, _start_evidence(*units_ready(at(30))), direction="candidate", now=at(40)
    )
    assert (verdict.action, verdict.affected, verdict.core) == ("proceed", (), ())


def test_start_proceeds_below_threshold_with_the_failed_unit_marked() -> None:
    cohort = two_units(gateway_agents=(1, 2, 3, 4, 5, 6, 7, 8, 9), runner_agents=(10,))
    evidence = _start_evidence(
        *units_ready(at(30), GATEWAY), UnitReport(unit=RUNNER, state="failed", observed_at=at(20))
    )
    verdict = judge_start(POLICY, cohort, evidence, direction="candidate", now=at(40))
    assert verdict.action == "proceed"
    assert verdict.failed_units == (RUNNER,)
    assert [(a.agent, a.reasons) for a in verdict.affected] == [(10, ("unit_failed",))]


def test_start_recovers_when_a_failed_unit_carries_more_than_the_threshold() -> None:
    evidence = _start_evidence(
        *units_ready(at(30), GATEWAY), UnitReport(unit=RUNNER, state="failed", observed_at=at(20))
    )
    verdict = judge_start(POLICY, TEN, evidence, direction="candidate", now=at(40))
    assert (verdict.action, verdict.threshold_exceeded) == ("recover", True)
    assert len(verdict.affected) == 5


def test_start_counts_a_silent_unit_as_unknown_and_affected() -> None:
    verdict = judge_start(
        POLICY,
        TEN,
        _start_evidence(*units_ready(at(30), GATEWAY)),
        direction="candidate",
        now=at(40),
    )
    assert verdict.unknown_units == (RUNNER,)
    assert {a.reasons for a in verdict.affected} == {("unit_unknown",)}
    assert verdict.action == "recover"


def test_start_ready_report_older_than_the_interval_is_unknown() -> None:
    stale = UnitReport(unit=RUNNER, state="ready", observed_at=at(-1))
    verdict = judge_start(
        POLICY,
        two_units(gateway_agents=(1,)),
        _start_evidence(*units_ready(at(30), GATEWAY), stale),
        direction="candidate",
        now=at(40),
    )
    assert verdict.unknown_units == (RUNNER,)
    assert verdict.action == "proceed"  # the runner carries no cohort agent


def test_start_recovers_on_a_failed_core_signal_with_no_agent_affected() -> None:
    refused = CoreReport(signal="pooler", ok=False, observed_at=at(35), detail="refused")
    core = (*_without(core_ok(at(30)), "pooler"), refused)
    verdict = judge_start(
        POLICY,
        two_units(),
        _start_evidence(*units_ready(at(30)), core=core),
        direction="candidate",
        now=at(40),
    )
    assert verdict.action == "recover"
    assert verdict.core == (CoreFailure(signal="pooler", state="failed", detail="refused"),)


def test_start_recovers_on_a_missing_core_signal() -> None:
    verdict = judge_start(
        POLICY,
        two_units(),
        _start_evidence(*units_ready(at(30)), core=_without(core_ok(at(30)), "redis")),
        direction="candidate",
        now=at(40),
    )
    assert verdict.action == "recover"
    assert verdict.core == (CoreFailure(signal="redis", state="unknown"),)


def test_a_failed_gateway_unit_is_a_core_failure_whatever_its_samples_say() -> None:
    evidence = _start_evidence(
        *units_ready(at(30), RUNNER), UnitReport(unit=GATEWAY, state="failed", observed_at=at(20))
    )
    verdict = judge_start(POLICY, two_units(), evidence, direction="candidate", now=at(40))
    assert verdict.action == "recover"
    assert verdict.core == (
        CoreFailure(signal="gateway", state="failed", detail="gateway unit failed"),
    )


def test_start_refuses_agent_samples_before_agents_resume() -> None:
    evidence = Evidence(
        since=RESUMED,
        units=units_ready(at(30)),
        agents=agents_live(at(30), (1,)),
        core=core_ok(at(30)),
    )
    with pytest.raises(ValueError, match="resume after the start barrier"):
        judge_start(POLICY, TEN, evidence, direction="candidate", now=at(40))


def test_start_holds_instead_of_recovering_twice() -> None:
    verdict = judge_start(
        POLICY,
        two_units(),
        _start_evidence(*units_ready(at(30)), core=_without(core_ok(at(30)), "database")),
        direction="previous",
        now=at(40),
    )
    assert verdict.action == "hold"


# --- watch window: before the end ----------------------------------------------------


def test_mid_window_with_nothing_definitive_keeps_watching() -> None:
    # No agent observations yet and no core samples: not judged until the end.
    verdict = judge_watch(POLICY, TEN, Evidence(since=RESUMED), direction="candidate", now=MID)
    assert (verdict.action, verdict.window_end) == ("watch", WATCH_END)
    assert (verdict.affected, verdict.core, verdict.unknown_units) == ((), (), ())


def test_mid_window_runtime_errors_above_threshold_recover_early() -> None:
    evidence = Evidence(since=RESUMED, agents=_errors((1, 2, 6)))
    verdict = judge_watch(POLICY, TEN, evidence, direction="candidate", now=MID)
    assert (verdict.action, verdict.threshold_exceeded) == ("recover", True)
    assert {a.reasons for a in verdict.affected} == {("runtime_error",)}


def test_mid_window_exactly_at_threshold_keeps_watching() -> None:
    evidence = Evidence(since=RESUMED, agents=_errors((1, 6)))
    verdict = judge_watch(POLICY, TEN, evidence, direction="candidate", now=MID)
    assert verdict.action == "watch"
    assert len(verdict.affected) == 2


def test_mid_window_core_failure_recovers_at_once() -> None:
    failed = CoreReport(signal="delivery", ok=False, observed_at=at(10))
    verdict = judge_watch(
        POLICY, TEN, Evidence(since=RESUMED, core=(failed,)), direction="candidate", now=MID
    )
    assert verdict.action == "recover"


def test_mid_window_quarantine_is_definitive() -> None:
    quarantined = tuple(
        AgentReport(agent=a, live=False, runtime_error=False, quarantined=True, observed_at=MID)
        for a in (6, 7, 8)
    )
    verdict = judge_watch(
        POLICY, TEN, Evidence(since=RESUMED, agents=quarantined), direction="candidate", now=MID
    )
    assert verdict.action == "recover"


def test_samples_before_resume_are_not_evidence_of_the_window() -> None:
    before = _errors((1, 2, 3), at(-5))
    failed_core = CoreReport(signal="redis", ok=False, observed_at=at(-5))
    verdict = judge_watch(
        POLICY,
        TEN,
        Evidence(since=RESUMED, agents=before, core=(failed_core,)),
        direction="candidate",
        now=MID,
    )
    assert verdict.action == "watch"


# --- watch window: at the end --------------------------------------------------------


def test_window_end_with_every_fresh_sample_ok_commits_clean() -> None:
    verdict = judge_watch(
        POLICY, TEN, healthy(WATCH_END, ALL), direction="candidate", now=WATCH_END
    )
    assert (verdict.action, verdict.outcome, verdict.exercised) == ("commit", "clean", True)


def test_one_second_before_the_end_is_still_watching() -> None:
    now = at(-1, WATCH_END)
    verdict = judge_watch(POLICY, TEN, healthy(now, ALL), direction="candidate", now=now)
    assert verdict.action == "watch"


def test_one_unobserved_agent_commits_degraded() -> None:
    verdict = judge_watch(
        POLICY, TEN, healthy(WATCH_END, ALL[1:]), direction="candidate", now=WATCH_END
    )
    assert (verdict.action, verdict.outcome) == ("commit", "degraded")
    assert [(a.agent, a.reasons) for a in verdict.affected] == [(1, ("unobserved",))]


def test_unobserved_agents_above_threshold_recover() -> None:
    verdict = judge_watch(
        POLICY, TEN, healthy(WATCH_END, ALL[3:]), direction="candidate", now=WATCH_END
    )
    assert (verdict.action, verdict.threshold_exceeded) == ("recover", True)


def test_an_agent_sample_before_the_end_does_not_vouch_for_the_end() -> None:
    evidence = healthy(WATCH_END, ALL[1:])
    evidence = evidence.model_copy(update={"agents": (*evidence.agents, *agents_live(MID, (1,)))})
    verdict = judge_watch(POLICY, TEN, evidence, direction="candidate", now=WATCH_END)
    assert [(a.agent, a.reasons) for a in verdict.affected] == [(1, ("unobserved",))]


def test_a_fresh_not_live_sample_is_affected() -> None:
    evidence = healthy(WATCH_END, ALL)
    dead = AgentReport(
        agent=4, live=False, runtime_error=False, quarantined=False, observed_at=WATCH_END
    )
    evidence = evidence.model_copy(update={"agents": (*evidence.agents, dead)})
    verdict = judge_watch(POLICY, TEN, evidence, direction="candidate", now=WATCH_END)
    assert [(a.agent, a.reasons) for a in verdict.affected] == [(4, ("not_live",))]


def test_a_core_sample_from_inside_the_window_is_unknown_at_the_end() -> None:
    evidence = healthy(WATCH_END, ALL)
    stale = (
        *_without(evidence.core, "schedules"),
        *(r for r in core_ok(MID) if r.signal == "schedules"),
    )
    verdict = judge_watch(
        POLICY,
        TEN,
        evidence.model_copy(update={"core": stale}),
        direction="candidate",
        now=WATCH_END,
    )
    assert verdict.action == "recover"
    assert verdict.core == (CoreFailure(signal="schedules", state="unknown"),)


def test_a_unit_silent_at_the_end_is_unknown() -> None:
    evidence = healthy(WATCH_END, ALL)
    evidence = evidence.model_copy(
        update={"units": (*units_ready(WATCH_END, GATEWAY), *units_ready(MID, RUNNER))}
    )
    verdict = judge_watch(POLICY, TEN, evidence, direction="candidate", now=WATCH_END)
    assert verdict.unknown_units == (RUNNER,)
    assert verdict.action == "recover"  # 5 of 10 cohort agents on it


def test_a_failed_mark_before_resume_stays_failed() -> None:
    cohort = two_units(gateway_agents=tuple(range(1, 10)), runner_agents=())
    evidence = healthy(WATCH_END, tuple(range(1, 10)))
    marked = UnitReport(unit=RUNNER, state="failed", observed_at=at(-120))
    evidence = evidence.model_copy(update={"units": (*units_ready(WATCH_END, GATEWAY), marked)})
    verdict = judge_watch(POLICY, cohort, evidence, direction="candidate", now=WATCH_END)
    assert (verdict.action, verdict.outcome, verdict.failed_units) == (
        "commit",
        "degraded",
        (RUNNER,),
    )


def test_a_small_cohort_does_not_roll_back_on_one_agent() -> None:
    cohort = two_units(gateway_agents=(1, 2, 3))
    verdict = judge_watch(
        POLICY, cohort, healthy(WATCH_END, (2, 3)), direction="candidate", now=WATCH_END
    )
    assert (verdict.action, verdict.outcome, verdict.threshold_exceeded) == (
        "commit",
        "degraded",
        False,
    )
    verdict = judge_watch(
        POLICY, cohort, healthy(WATCH_END, (3,)), direction="candidate", now=WATCH_END
    )
    assert verdict.action == "recover"


def test_an_empty_cohort_decides_on_units_and_core_and_proves_no_workload() -> None:
    cohort = two_units()
    verdict = judge_watch(
        POLICY, cohort, healthy(WATCH_END, ()), direction="candidate", now=WATCH_END
    )
    assert (verdict.action, verdict.outcome, verdict.exercised) == ("commit", "clean", False)
    bare = Evidence(since=RESUMED, units=units_ready(WATCH_END))
    verdict = judge_watch(POLICY, cohort, bare, direction="candidate", now=WATCH_END)
    assert verdict.action == "recover"  # every core signal unknown
    assert len(verdict.core) == 8


def test_watch_failure_on_the_previous_direction_holds() -> None:
    verdict = judge_watch(POLICY, TEN, healthy(WATCH_END, ()), direction="previous", now=WATCH_END)
    assert verdict.action == "hold"


def test_policy_window_and_threshold_are_honoured() -> None:
    policy = FleetPolicy(watch_s=60, threshold_percent=0, min_affected=1)
    now = at(60)
    verdict = judge_watch(policy, TEN, healthy(now, ALL[1:]), direction="candidate", now=now)
    assert (verdict.action, verdict.window_end) == ("recover", now)


# --- evidence coherence --------------------------------------------------------------


def test_evidence_from_the_future_is_a_coordinator_defect() -> None:
    with pytest.raises(ValueError, match="newer than the verdict"):
        judge_watch(POLICY, TEN, healthy(at(61), ALL), direction="candidate", now=at(60))


def test_a_verdict_cannot_precede_its_interval() -> None:
    with pytest.raises(ValueError, match="precede"):
        judge_watch(POLICY, TEN, Evidence(since=RESUMED), direction="candidate", now=at(-1))


def test_evidence_about_strangers_is_refused() -> None:
    stranger = AgentReport(
        agent=99, live=True, runtime_error=False, quarantined=False, observed_at=MID
    )
    with pytest.raises(ValueError, match="outside the frozen cohort"):
        judge_watch(
            POLICY, TEN, Evidence(since=RESUMED, agents=(stranger,)), direction="candidate", now=MID
        )
    other = UnitReport(
        unit=RUNNER.model_copy(update={"machine": "x"}), state="ready", observed_at=MID
    )
    with pytest.raises(ValueError, match="outside the operation"):
        judge_watch(
            POLICY, TEN, Evidence(since=RESUMED, units=(other,)), direction="candidate", now=MID
        )


# --- the journaled verdict -----------------------------------------------------------


def test_verdict_round_trips_through_json() -> None:
    verdict = judge_watch(
        POLICY, TEN, healthy(WATCH_END, ALL[3:]), direction="candidate", now=WATCH_END
    )
    assert Verdict.model_validate_json(verdict.model_dump_json()) == verdict


@pytest.mark.parametrize(
    "fields",
    [
        {"action": "recover", "direction": "previous", "threshold_exceeded": True},
        {"action": "commit"},  # no outcome
        {"action": "watch", "outcome": "clean"},
        {"action": "recover"},  # nothing failed
        {"action": "commit", "outcome": "clean", "threshold_exceeded": True},
        {"action": "proceed"},  # not a watch action
        {"window_end": None},
    ],
)
def test_incoherent_verdicts_are_refused(fields: dict[str, object]) -> None:
    base: dict[str, object] = {
        "stage": "watch",
        "direction": "candidate",
        "action": "watch",
        "decided_at": MID,
        "window_end": WATCH_END,
        "cohort_size": 10,
    }
    with pytest.raises(ValidationError):
        Verdict.model_validate(base | fields)


def test_a_window_journals_each_agents_first_sighting_of_each_fact_once() -> None:
    """A fact already sighted in the window adds nothing; a new fact of the
    same agent does; a sighting from before the window is no longer known."""
    error = _errors((1,), at(40))[0]
    quarantine = error.model_copy(update={"runtime_error": False, "quarantined": True})
    later = _errors((1, 2), at(70))
    assert first_sightings((), (error,), since=RESUMED) == (error,)
    assert first_sightings((error,), later, since=RESUMED) == (later[1],)
    assert first_sightings((error,), (quarantine,), since=RESUMED) == (quarantine,)
    assert first_sightings((error,), later, since=at(50)) == later
    assert first_sightings((), agents_live(at(70), (1, 2)), since=RESUMED) == ()
