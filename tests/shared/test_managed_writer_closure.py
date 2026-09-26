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
    ExpectedSession,
    ExpectedUnitWriters,
    SessionVerdict,
)
from shared.native_job_observation import LauncherObservation
from shared.process_evidence import ExpectedProcess, ProcessVerdict

DIGEST = "a" * 64
OTHER_DIGEST = "b" * 64
NEW_DIGEST = "c" * 64
RECEIPT_DIGEST = "9" * 64
OTHER_RECEIPT_DIGEST = "8" * 64
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
    return LauncherObservation(
        kind="launchd", definition="absent", loaded=False, current_digest=None
    )


def _rebound_facts(digest: str = NEW_DIGEST) -> LauncherObservation:
    return LauncherObservation(
        kind="launchd", definition="mismatch", loaded=False, current_digest=digest
    )


def _assemble(
    *,
    expected: ExpectedUnitWriters | None = None,
    launchers: tuple[LauncherObservation, ...] | None = None,
    terminals: tuple[LauncherTerminal, ...] | None = None,
    processes: tuple[ProcessVerdict, ...] = ("exited",),
    sessions: tuple[SessionVerdict, ...] = ("absent",),
    observed_unit: ManagedUnit | None = None,
    echoed_challenge: UUID | None = None,
    prepared_receipt_digest: str = RECEIPT_DIGEST,
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
        prepared_receipt_digest=prepared_receipt_digest,
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
    facts = LauncherObservation(
        kind="launchd", definition="absent", loaded=loaded, current_digest=None
    )
    assert not launcher_fenced(facts, _removed_terminal())


def test_launcher_fenced_refuses_present_definition_and_crossed_ledgers() -> None:
    assert not launcher_fenced(
        LauncherObservation(
            kind="launchd", definition="match", loaded=False, current_digest=DIGEST
        ),
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
    plan = _expected()
    assert first.unit == ManagedUnit(
        machine=plan.machine, home=plan.home, inventory_digest=RECEIPT_DIGEST
    )
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
    assert (
        _assemble(
            launchers=(LauncherObservation(kind="launchd", definition="absent", loaded=loaded),)
        )
        is None
    )


def test_pin05_present_definition_refuses() -> None:
    assert (
        _assemble(
            launchers=(
                LauncherObservation(
                    kind="launchd", definition="match", loaded=False, current_digest=DIGEST
                ),
            )
        )
        is None
    )


def test_pin06_window_refusals() -> None:
    assert _assemble(observed_at=BASE + timedelta(minutes=10)) is None  # observed_at >= valid_until
    assert (
        _assemble(observed_at=BASE - timedelta(minutes=1)) is None
    )  # before operation acquisition


def test_crontab_fallback_facts_refuse_the_fence() -> None:
    """The native-read failure fallback (`LauncherObservation(kind=...)`) admits
    nothing (i1 QA N1): a crontab-expected unit whose facts are the unknown
    fallback refuses the fence and the unit closure."""
    expected = _expected(
        launchers=(ExpectedLauncher(kind="crontab", name=LABEL, definition_digest=DIGEST),)
    )
    fallback = LauncherObservation(kind="crontab")

    assert not launcher_fenced(fallback, _removed_terminal())
    assert (
        _assemble(expected=expected, launchers=(fallback,), terminals=(_removed_terminal(),))
        is None
    )


def test_pin07_unknown_facts_refuse() -> None:
    assert (
        _assemble(
            launchers=(LauncherObservation(kind="launchd", definition="unknown", loaded=False),)
        )
        is None
    )


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
                LauncherObservation(
                    kind="launchd", definition="match", loaded=False, current_digest=DIGEST
                ),
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


def test_receipt_digest_binding_stamps_and_freezes_both_classes() -> None:
    """#4129 Q1: the closure unit carries the full receipt digest; the
    observer-tuple attribution stays checked; both classes are frozen into
    ``observation_digest``."""
    plan = _expected()
    assert plan.unit().inventory_digest != RECEIPT_DIGEST  # distinct classes (production shape)
    first = _assemble(expected=plan, prepared_receipt_digest=RECEIPT_DIGEST)
    second = _assemble(expected=plan, prepared_receipt_digest=OTHER_RECEIPT_DIGEST)
    assert first is not None and second is not None
    assert first.unit == ManagedUnit(machine="runner", home="/ava", inventory_digest=RECEIPT_DIGEST)
    assert second.unit.inventory_digest == OTHER_RECEIPT_DIGEST
    assert first.observation_digest != second.observation_digest
    assert _assemble(expected=plan, prepared_receipt_digest=RECEIPT_DIGEST) == first

    # The observer-tuple digest class is inside the frozen payload too: with
    # everything else identical, only the expected tuple changing (here the
    # artifact digest, which reaches the payload through ``expected.unit()``)
    # moves the observation digest.
    varied = _expected().model_copy(update={"artifact_digest": "0" * 64})
    third = _assemble(expected=varied, prepared_receipt_digest=RECEIPT_DIGEST)
    assert third is not None
    assert (third.unit.machine, third.unit.home) == (first.unit.machine, first.unit.home)
    assert third.observation_digest != first.observation_digest


def test_crontab_fence_removed_only_through_the_table_fact() -> None:
    """#4129 Q10: crontab has no separate loaded state; absent (double-read
    stable) + not enabled is the complete fact, and rebound refuses."""
    removed = LauncherObservation(kind="crontab", definition="absent", enabled=False)
    assert removed.loaded is None and removed.current_digest is None
    assert launcher_fenced(removed, _removed_terminal(DIGEST))
    assert not launcher_fenced(
        removed, LauncherTerminal(label=DIGEST, kind="rebound", new_digest=NEW_DIGEST)
    )
    assert not launcher_fenced(
        LauncherObservation(kind="crontab", definition="match", enabled=True),
        _removed_terminal(DIGEST),
    )
    assert not launcher_fenced(
        LauncherObservation(kind="crontab", definition="absent", enabled=False, loaded=False),
        _removed_terminal(DIGEST),
    )
    assert not launcher_fenced(
        LauncherObservation(kind="crontab", definition="absent", enabled=None),
        _removed_terminal(DIGEST),
    )
    assert not launcher_fenced(LauncherObservation(kind="schtasks"), _removed_terminal(LABEL))


def test_crontab_unit_closure_positive_and_kind_drift_refuses() -> None:
    crontab_launcher = ExpectedLauncher(kind="crontab", name=DIGEST, definition_digest=DIGEST)
    crontab_expected = _expected(launchers=(crontab_launcher,))
    crontab_facts = LauncherObservation(kind="crontab", definition="absent", enabled=False)
    closure = _assemble(
        expected=crontab_expected,
        launchers=(crontab_facts,),
        terminals=(_removed_terminal(DIGEST),),
    )
    assert closure is not None and closure.outcome == "old_writers_absent_relaunchers_fenced"
    assert (
        _assemble(
            expected=crontab_expected,
            launchers=(_removed_facts(),),  # launchd fact against a crontab entry
            terminals=(_removed_terminal(DIGEST),),
        )
        is None
    )
