"""Known-good publication and the rejected-candidate oscillation guard."""

from __future__ import annotations

from uuid import UUID

import pytest
from pydantic import ValidationError

from cli.release_fleet.policy import UnitKey
from cli.release_fleet.publication import (
    Completion,
    FleetRelease,
    FleetState,
    Rejection,
    publish,
    require_admissible,
)
from cli.release_fleet.workload import judge_watch
from tests.lifecycle.release_fleet.conftest import (
    GATEWAY,
    OPERATION,
    POLICY,
    RUNNER,
    WATCH_END,
    at,
    healthy,
    two_units,
    units_ready,
)

A = FleetRelease(source_commit="a" * 40, schema_digest="1" * 64, sql_inventory_digest="2" * 64)
B = FleetRelease(source_commit="b" * 40, schema_digest="1" * 64, sql_inventory_digest="2" * 64)
LATER = UUID("00000000-0000-4000-8000-000000000002")
PAUSED = UnitKey(machine="win", home="/c/Users/zzy/.ava")
PRIOR = FleetState(
    operation=UUID("00000000-0000-4000-8000-000000000000"),
    updated_at=at(-86400),
    current=A,
    last_known_good=A,
)


def _completion(outcome: str, *, exercised: bool = False) -> Completion:
    return Completion.model_validate(
        {
            "operation": OPERATION,
            "at": at(10),
            "outcome": outcome,
            "previous": A,
            "candidate": B,
            "exercised": exercised,
        }
    )


def test_an_exercised_clean_commit_becomes_last_known_good() -> None:
    state = publish(PRIOR, _completion("clean", exercised=True))
    assert state is not None
    assert (state.current, state.last_known_good, state.operation) == (B, B, OPERATION)
    assert FleetState.model_validate_json(state.model_dump_json()) == state


def test_a_clean_commit_no_workload_exercised_keeps_the_known_good() -> None:
    state = publish(PRIOR, _completion("clean"))
    assert state is not None
    assert (state.current, state.last_known_good) == (B, A)


def test_a_degraded_commit_never_becomes_known_good() -> None:
    state = publish(PRIOR, _completion("degraded"))
    assert state is not None
    assert (state.current, state.last_known_good) == (B, A)


def test_a_recovery_stays_on_previous_and_rejects_the_candidate() -> None:
    state = publish(PRIOR, _completion("recovered"))
    assert state is not None
    assert (state.current, state.last_known_good) == (A, A)
    assert state.rejected == (Rejection(release=B, operation=OPERATION, at=at(10)),)


def test_an_abort_publishes_nothing() -> None:
    assert publish(PRIOR, _completion("aborted")) is None
    assert publish(None, _completion("aborted")) is None


def test_the_first_fleet_operation_has_no_prior_state() -> None:
    state = publish(None, _completion("recovered"))
    assert state is not None
    assert (state.current, state.last_known_good, len(state.rejected)) == (A, None, 1)


def test_publication_refuses_an_operation_that_did_not_start_from_current() -> None:
    moved = PRIOR.model_copy(update={"current": B})
    with pytest.raises(ValueError, match="did not start from the published current"):
        publish(moved, _completion("clean", exercised=True))


@pytest.mark.parametrize(
    "fields",
    [
        {"outcome": "degraded", "exercised": True},
        {"outcome": "recovered", "exercised": True},
        {"candidate": A},
        {"stale_units": (GATEWAY, RUNNER)},  # not sorted
    ],
)
def test_incoherent_completions_are_refused(fields: dict[str, object]) -> None:
    base: dict[str, object] = {
        "operation": OPERATION,
        "at": at(10),
        "outcome": "clean",
        "previous": A,
        "candidate": B,
    }
    with pytest.raises(ValidationError):
        Completion.model_validate(base | fields)


def test_a_commit_verdict_completes_with_its_failed_units_and_exclusions_stale() -> None:
    cohort = two_units(gateway_agents=(1, 2, 3))
    evidence = healthy(WATCH_END, (1, 2, 3))
    evidence = evidence.model_copy(update={"units": units_ready(WATCH_END, GATEWAY)})
    verdict = judge_watch(POLICY, cohort, evidence, direction="candidate", now=WATCH_END)
    assert (verdict.action, verdict.outcome) == ("commit", "degraded")
    completion = Completion.committed(
        verdict, operation=OPERATION, previous=A, candidate=B, excluded=(PAUSED,)
    )
    assert completion.stale_units == (RUNNER, PAUSED)
    assert (completion.outcome, completion.exercised, completion.at) == (
        "degraded",
        False,
        WATCH_END,
    )


def test_a_clean_commit_verdict_carries_its_workload_proof() -> None:
    cohort = two_units(gateway_agents=(1,))
    verdict = judge_watch(
        POLICY, cohort, healthy(WATCH_END, (1,)), direction="candidate", now=WATCH_END
    )
    completion = Completion.committed(
        verdict, operation=OPERATION, previous=A, candidate=B, excluded=()
    )
    assert (completion.outcome, completion.exercised) == ("clean", True)
    empty = judge_watch(
        POLICY, two_units(), healthy(WATCH_END, ()), direction="candidate", now=WATCH_END
    )
    unproven = Completion.committed(
        empty, operation=OPERATION, previous=A, candidate=B, excluded=()
    )
    assert (unproven.outcome, unproven.exercised) == ("clean", False)


def test_only_a_commit_verdict_completes_on_the_candidate() -> None:
    watching = judge_watch(
        POLICY, two_units(), healthy(at(5), ()), direction="candidate", now=at(5)
    )
    with pytest.raises(ValueError, match="only a commit verdict"):
        Completion.committed(watching, operation=OPERATION, previous=A, candidate=B, excluded=())


def test_a_fresh_candidate_needs_no_acknowledgement() -> None:
    require_admissible(PRIOR, B, None)
    require_admissible(None, B, None)


def test_an_acknowledgement_naming_no_rejection_is_refused() -> None:
    with pytest.raises(ValueError, match="does not reject this candidate"):
        require_admissible(PRIOR, B, OPERATION)


def test_a_rejected_candidate_needs_the_latest_rejection_acknowledged() -> None:
    rejected = publish(PRIOR, _completion("recovered"))
    assert rejected is not None
    with pytest.raises(ValueError, match="must acknowledge"):
        require_admissible(rejected, B, None)
    require_admissible(rejected, B, OPERATION)
    again = rejected.model_copy(
        update={"rejected": (*rejected.rejected, Rejection(release=B, operation=LATER, at=at(99)))}
    )
    with pytest.raises(ValueError, match=str(LATER)):
        require_admissible(again, B, OPERATION)
    require_admissible(again, B, LATER)
    # The rejection of B says nothing about another candidate.
    require_admissible(again, A, None)
