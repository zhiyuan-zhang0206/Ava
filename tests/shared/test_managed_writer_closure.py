"""Derivation pins for `shared.managed_writer_closure` (design 5.2/5.3, pins 1-9).

Pure facts-in / closure-out checks: the storage suite already covers adoption;
these pin the derivation's fail-closed default and every refusal class.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from shared.managed_writer_barrier import ManagedUnit, ManagedUnitClosure, RolloutIdentity
from shared.managed_writer_closure import (
    LauncherTerminal,
    assemble_collection,
    assemble_unit_closure,
    launcher_fenced,
)
from shared.managed_writer_observation import (
    ExpectedLauncher,
    ExpectedProcess,
    ExpectedSession,
    ExpectedUnitWriters,
    ProcessVerdict,
    SessionVerdict,
)
from shared.native_job_observation import LauncherObservation

DIGEST = "a" * 64
OTHER_DIGEST = "b" * 64
NEW_DIGEST = "c" * 64
LABEL = "com.ava.test"
LABEL_TWO = "com.ava.test.two"
BASE = datetime(2026, 9, 20, 4, 0, tzinfo=UTC)


def _uuid(n: int) -> UUID:
    return UUID(int=n)


def _operation(acquired_at: datetime = BASE) -> RolloutIdentity:
    return RolloutIdentity(holder="runner:1", acquired_at=acquired_at, target_sha="e" * 40)


def _expected(
    *, home: str = "/ava", launchers: tuple[ExpectedLauncher, ...] | None = None
) -> ExpectedUnitWriters:
    launchers = (
        (ExpectedLauncher(kind="launchd", name=LABEL, definition_digest=DIGEST),)
        if launchers is None
        else launchers
    )
    process = ExpectedProcess(pid=41, create_time=1.0)
    return ExpectedUnitWriters(
        machine="runner",
        home=home,
        artifact_digest="d" * 64,
        manifest_digest="f" * 64,
        processes=(process,),
        sessions=(ExpectedSession(name="ava-ops", process=process),),
        launchers=launchers,
    )


def _removed_terminal(label: str = LABEL) -> LauncherTerminal:
    return LauncherTerminal(label=label, kind="removed")


def _removed_facts() -> LauncherObservation:
    return LauncherObservation(definition="absent", loaded=False, current_digest=None)


def _rebound_facts(digest: str = NEW_DIGEST) -> LauncherObservation:
    return LauncherObservation(definition="mismatch", loaded=False, current_digest=digest)


def _assemble(
    *,
    expected: ExpectedUnitWriters | None = None,
    launchers: tuple[LauncherObservation, ...] | None = None,
    terminals: tuple[LauncherTerminal, ...] | None = None,
    processes: tuple[ProcessVerdict, ...] = ("exited",),
    sessions: tuple[SessionVerdict, ...] = ("absent",),
    observed_unit: ManagedUnit | None = None,
    echoed_challenge: UUID | None = None,
    observed_at: datetime = BASE + timedelta(minutes=1),
    valid_until: datetime = BASE + timedelta(minutes=10),
) -> ManagedUnitClosure | None:
    plan = _expected() if expected is None else expected
    return assemble_unit_closure(
        plan,
        operation=_operation(),
        operation_challenge=_uuid(1),
        echoed_challenge=_uuid(1) if echoed_challenge is None else echoed_challenge,
        boot_id=_uuid(2),
        observer_instance=_uuid(3),
        observed_unit=plan.unit() if observed_unit is None else observed_unit,
        observed_at=observed_at,
        valid_until=valid_until,
        processes=processes,
        sessions=sessions,
        launchers=(_removed_facts(),) if launchers is None else launchers,
        terminals=(_removed_terminal(),) if terminals is None else terminals,
    )


def test_launcher_fenced_removed_and_rebound_positives() -> None:
    assert launcher_fenced(_removed_facts(), _removed_terminal())
    assert launcher_fenced(
        _rebound_facts(), LauncherTerminal(label=LABEL, kind="rebound", new_digest=NEW_DIGEST)
    )


@pytest.mark.parametrize("loaded", [True, None])
def test_launcher_fenced_refuses_unproven_unloaded(loaded: bool | None) -> None:
    facts = LauncherObservation(definition="absent", loaded=loaded, current_digest=None)
    assert not launcher_fenced(facts, _removed_terminal())


def test_launcher_fenced_refuses_present_definition_and_crossed_ledgers() -> None:
    assert not launcher_fenced(
        LauncherObservation(definition="match", loaded=False, current_digest=DIGEST),
        _removed_terminal(),
    )
    assert not launcher_fenced(
        _removed_facts(), LauncherTerminal(label=LABEL, kind="rebound", new_digest=NEW_DIGEST)
    )
    assert not launcher_fenced(_rebound_facts(), _removed_terminal())
    assert not launcher_fenced(
        _rebound_facts(OTHER_DIGEST),
        LauncherTerminal(label=LABEL, kind="rebound", new_digest=NEW_DIGEST),
    )


def test_terminal_digest_coherence() -> None:
    with pytest.raises(ValueError, match="new definition digest"):
        LauncherTerminal(label=LABEL, kind="rebound")
    with pytest.raises(ValueError, match="no new digest"):
        LauncherTerminal(label=LABEL, kind="removed", new_digest=NEW_DIGEST)


def test_unit_closure_removed_positive_with_stable_digest() -> None:
    first = _assemble()
    second = _assemble()
    assert first is not None and second is not None
    assert first == second
    assert first.outcome == "old_writers_absent_relaunchers_fenced"
    assert first.unit == _expected().unit()
    assert len(first.observation_digest) == 64

    moved = _assemble(observed_at=BASE + timedelta(minutes=2))
    assert moved is not None
    assert moved.observation_digest != first.observation_digest


def test_unit_closure_rebound_positive() -> None:
    closure = _assemble(
        launchers=(_rebound_facts(),),
        terminals=(LauncherTerminal(label=LABEL, kind="rebound", new_digest=NEW_DIGEST),),
    )
    assert closure is not None
    assert closure.outcome == "old_writers_absent_relaunchers_fenced"


def test_pin01_absent_facts_against_rebound_ledger_refuse() -> None:
    assert (
        _assemble(terminals=(LauncherTerminal(label=LABEL, kind="rebound", new_digest=NEW_DIGEST),))
        is None
    )


def test_pin01_rebound_facts_against_removed_ledger_refuse() -> None:
    assert _assemble(launchers=(_rebound_facts(),)) is None


def test_pin02_rebound_digest_mismatch_refuses() -> None:
    assert (
        _assemble(
            launchers=(_rebound_facts(OTHER_DIGEST),),
            terminals=(LauncherTerminal(label=LABEL, kind="rebound", new_digest=NEW_DIGEST),),
        )
        is None
    )


@pytest.mark.parametrize("loaded", [True, None])
def test_pin03_pin04_loaded_not_false_refuses(loaded: bool | None) -> None:
    assert _assemble(launchers=(LauncherObservation(definition="absent", loaded=loaded),)) is None


def test_pin05_present_definition_refuses() -> None:
    assert (
        _assemble(
            launchers=(
                LauncherObservation(definition="match", loaded=False, current_digest=DIGEST),
            )
        )
        is None
    )


def test_pin06_window_refusals() -> None:
    assert _assemble(observed_at=BASE + timedelta(minutes=10)) is None  # observed_at >= valid_until
    assert (
        _assemble(observed_at=BASE - timedelta(minutes=1)) is None
    )  # before operation acquisition


def test_pin07_unknown_facts_refuse() -> None:
    assert _assemble(launchers=(LauncherObservation(definition="unknown", loaded=False),)) is None


def test_pin08_challenge_replay_refuses() -> None:
    assert _assemble(echoed_challenge=_uuid(9)) is None


def test_pin09_one_unfenced_launcher_refuses_the_unit() -> None:
    expected = _expected(
        launchers=(
            ExpectedLauncher(kind="launchd", name=LABEL, definition_digest=DIGEST),
            ExpectedLauncher(kind="launchd", name=LABEL_TWO, definition_digest=DIGEST),
        )
    )
    assert (
        _assemble(
            expected=expected,
            launchers=(
                _removed_facts(),
                LauncherObservation(definition="match", loaded=False, current_digest=DIGEST),
            ),
            terminals=(_removed_terminal(LABEL), _removed_terminal(LABEL_TWO)),
        )
        is None
    )


def test_old_writer_verdicts_gate_the_closure() -> None:
    assert _assemble(processes=("alive",)) is None
    assert _assemble(processes=()) is None
    assert _assemble(sessions=("record_present",)) is None
    assert _assemble(sessions=()) is None


def test_terminal_coverage_and_observed_unit_must_match() -> None:
    assert _assemble(terminals=()) is None
    assert _assemble(terminals=(_removed_terminal(LABEL), _removed_terminal(LABEL))) is None
    assert _assemble(terminals=(_removed_terminal("com.ava.other"),)) is None
    assert (
        _assemble(
            observed_unit=ManagedUnit(machine="runner", home="/ava", inventory_digest="a" * 64)
        )
        is None
    )


def _closure(*, home: str = "/ava") -> ManagedUnitClosure:
    closure = _assemble(expected=_expected(home=home))
    assert closure is not None
    return closure


def test_collection_assembly_covers_exactly_the_registered_units() -> None:
    unit_one = _expected(home="/ava").unit()
    unit_two = _expected(home="/ava-two").unit()
    closure_one = _closure()
    closure_two = _closure(home="/ava-two")

    collection = assemble_collection(
        operation=_operation(),
        candidate_digest="a" * 64,
        challenge=_uuid(1),
        collected_at=BASE + timedelta(minutes=2),
        valid_until=BASE + timedelta(minutes=10),
        expected_units=(unit_one, unit_two),
        closures=(closure_one, closure_two),
    )
    assert collection is not None
    assert collection.units == (closure_one, closure_two)

    assert (
        assemble_collection(
            operation=_operation(),
            candidate_digest="a" * 64,
            challenge=_uuid(1),
            collected_at=BASE + timedelta(minutes=2),
            valid_until=BASE + timedelta(minutes=10),
            expected_units=(unit_one, unit_two),
            closures=(closure_one,),
        )
        is None
    )
    assert (
        assemble_collection(
            operation=_operation(),
            candidate_digest="a" * 64,
            challenge=_uuid(1),
            collected_at=BASE + timedelta(minutes=2),
            valid_until=BASE + timedelta(minutes=10),
            expected_units=(unit_one,),
            closures=(closure_two,),
        )
        is None
    )


def test_collection_window_and_emptiness_refuse() -> None:
    unit_one = _expected().unit()
    closure_one = _closure()
    assert (
        assemble_collection(
            operation=_operation(),
            candidate_digest="a" * 64,
            challenge=_uuid(1),
            collected_at=BASE - timedelta(minutes=1),
            valid_until=BASE + timedelta(minutes=10),
            expected_units=(unit_one,),
            closures=(closure_one,),
        )
        is None
    )
    assert (
        assemble_collection(
            operation=_operation(),
            candidate_digest="a" * 64,
            challenge=_uuid(1),
            collected_at=BASE + timedelta(minutes=2),
            valid_until=BASE + timedelta(minutes=2),
            expected_units=(unit_one,),
            closures=(closure_one,),
        )
        is None
    )
    assert (
        assemble_collection(
            operation=_operation(),
            candidate_digest="a" * 64,
            challenge=_uuid(1),
            collected_at=BASE + timedelta(minutes=2),
            valid_until=BASE + timedelta(minutes=10),
            expected_units=(),
            closures=(),
        )
        is None
    )
