"""`cli.commands._managed_writer_dispatch` + `_release_context` -- seal, targets, dispatch gate.

The begin position's second half: the sealed release context is read exactly
once, the all-unit plan is assembled and canonicalized byte-identically, the
candidate digest has its single derivation point (Q3, with an example-vector
pin), and the dispatch gate opens only when every unit acknowledges the exact
sealed digests. Everything here runs outside a database: the only database
touch the begin chain has is the journal registration read, stubbed in the
chain world below (task #4129 I5).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from cli.commands._managed_writer_collector import JournaledRegistration
from cli.commands._managed_writer_dispatch import (
    _ack_bindings,
    assemble_phase_inputs,
    begin_valid_until,
    build_targets,
    derive_candidate_digest,
    dispatch_prepared_plan,
    seal_operator_plan,
)
from cli.commands._managed_writer_gather import PreparedFactTarget, PreparedUnitFacts
from cli.commands._release_context import (
    ReleaseContext,
    ReleaseContextUnit,
    read_release_context,
    release_context_bytes,
    release_context_path,
)
from cli.commands._update_bootstrap import BootstrapHopRequest
from cli.commands._update_publication import (
    PreparedUnitPublication,
    build_pending_publication,
    published_unit,
)
from ops.cluster_rpc import ClusterOpFailed, ClusterOpUnreachable
from ops.rpc_prepare_dispatch import PrepareDispatchResult, ProjectionFile, prepared_hop_name
from ops.rpc_prepare_facts import ImageRef, RestrictedHopMaterial
from services.agent_ops.bootstrap import PreparedObservation
from shared.managed_writer_barrier import ManagedWriterBarrierError, RolloutIdentity
from shared.managed_writer_observation import (
    ExpectedProcess,
    ExpectedUnitWriters,
    ObservationChallenge,
)
from shared.managed_writer_publication import CandidateUnitPlan, NormalService, PublishedUnit
from shared.runtime_publication_input import PreparationReceipt, PreparedService
from shared.runtime_release import ReleaseRejectedError

ARTIFACT = "a" * 64
MANIFEST = "b" * 64
SCHEMA = "c" * 64
RECOVERY_ARTIFACT = "d" * 64
RECOVERY_MANIFEST = "e" * 64
RECOVERY_SCHEMA = "f" * 64
TARGET_SHA = "0" * 40
VALID_UNTIL = datetime(2026, 9, 20, 18, 0, tzinfo=UTC)
JOURNAL_CHALLENGE = UUID(int=4242)
CANDIDATE_DIGEST = hashlib.sha256(b"candidate-digest-fixture").hexdigest()
JOURNALED_V = datetime(2026, 9, 20, 3, 0, tzinfo=UTC)


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _receipt(machine: str, home: str) -> PreparationReceipt:
    expected = ExpectedUnitWriters(
        machine=machine,
        home=home,
        artifact_digest=ARTIFACT,
        manifest_digest=MANIFEST,
        processes=(),
        sessions=(),
        launchers=(),
    )
    return PreparationReceipt(
        version=1,
        expected=expected,
        services=(PreparedService(session="ava-ops", requires_db=True, gate=None),),
        excluded_registrations=(),
        inventory_digest=expected.unit().inventory_digest,
        closure="unknown",
        unresolved=("writer closure",),
    )


def _published(
    machine: str, home: str, *, artifact: str = ARTIFACT, manifest: str = MANIFEST
) -> PublishedUnit:
    receipt = _receipt(machine, home)
    body = receipt.model_dump_json().encode("ascii")
    return PublishedUnit(
        machine=machine,
        home=home,
        inventory_digest=receipt.expected.unit().inventory_digest,
        prepared_receipt_digest=hashlib.sha256(body).hexdigest(),
        artifact_digest=artifact,
        manifest_digest=manifest,
    )


def _candidate(unit: PublishedUnit) -> CandidateUnitPlan:
    image = f"{unit.home}/releases/{unit.artifact_digest}"
    selector = {
        "version": 2,
        "artifact_digest": unit.artifact_digest,
        "manifest_digest": unit.manifest_digest,
        "prepared_receipt_digest": unit.prepared_receipt_digest,
    }
    return CandidateUnitPlan(
        unit=unit,
        services=(
            NormalService(
                session="ava-ops",
                module="services.agent_ops.daemon",
                executable=f"{image}/venv/bin/python",
                entrypoint=f"{image}/venv/services/agent_ops/daemon.py",
                command_digest=_digest("command"),
            ),
        ),
        previous_selector_digest=None,
        selector_digest=hashlib.sha256(
            (json.dumps(selector, sort_keys=True, separators=(",", ":")) + "\n").encode()
        ).hexdigest(),
    )


def _hop_material(machine: str, home: str) -> RestrictedHopMaterial:
    context = PreparedObservation(
        expected=ExpectedUnitWriters(
            machine=machine,
            home=home,
            artifact_digest=RECOVERY_ARTIFACT,
            manifest_digest=RECOVERY_MANIFEST,
            processes=(),
            sessions=(),
            launchers=(),
        ),
        operation=RolloutIdentity(
            holder="gateway:pid1",
            acquired_at=datetime(2026, 9, 20, tzinfo=UTC),
            target_sha=TARGET_SHA,
        ),
        challenge=ObservationChallenge(
            challenge=UUID(int=1), valid_until=datetime(2026, 9, 20, 1, tzinfo=UTC)
        ),
        schema_digest=RECOVERY_SCHEMA,
    )
    return RestrictedHopMaterial(
        predecessor=ExpectedProcess(pid=424242, create_time=1700000000.0),
        recovery_context_path=f"{home}/run/hop-recovery-fixture.json",
        recovery_context=context.model_dump_json(),
    )


def _facts(machine: str, home: str, *, candidate: bool = True) -> PreparedUnitFacts:
    unit = _published(machine, home)
    return PreparedUnitFacts(
        publication=PreparedUnitPublication(
            receipt=_receipt(machine, home),
            prepared_receipt_digest=unit.prepared_receipt_digest,
            artifact_digest=unit.artifact_digest,
            manifest_digest=unit.manifest_digest,
            candidate=_candidate(unit) if candidate else None,
        ),
        previous_selector=None,
        recovery=ImageRef(
            artifact_digest=RECOVERY_ARTIFACT,
            manifest_digest=RECOVERY_MANIFEST,
            schema_digest=RECOVERY_SCHEMA,
        ),
        hop_material=_hop_material(machine, home),
    )


def _context(*pairs: tuple[str, str], schema_digest: str = SCHEMA) -> ReleaseContext:
    units = tuple(
        ReleaseContextUnit(
            machine=machine,
            candidate=ImageRef(
                artifact_digest=ARTIFACT, manifest_digest=MANIFEST, schema_digest=schema_digest
            ),
            recovery=PublishedUnit(
                machine=machine,
                home=home,
                inventory_digest=_digest(f"inventory:{machine}"),
                prepared_receipt_digest=_digest(f"receipt:{machine}"),
                artifact_digest=RECOVERY_ARTIFACT,
                manifest_digest=RECOVERY_MANIFEST,
            ),
            recovery_schema_digest=RECOVERY_SCHEMA,
        )
        for machine, home in sorted(pairs)
    )
    return ReleaseContext(
        version=1,
        target_sha=TARGET_SHA,
        schema_digest=schema_digest,
        applied_names=("one", "two"),
        units=units,
    )


def _operation() -> RolloutIdentity:
    return RolloutIdentity(
        holder="gateway:pid77", acquired_at=datetime.now(UTC), target_sha=TARGET_SHA
    )


def _seal(facts: list[PreparedUnitFacts], context: ReleaseContext) -> Any:
    return seal_operator_plan(
        facts,
        context,
        operation=_operation(),
        valid_until=VALID_UNTIL,
        coordinator_machine="runner-a",
        coordinator_home="/ava-a",
    )


# ── the sealed release context ───────────────────────────────────────────────


def _write_context(home: Path, context: ReleaseContext, *, mode: int = 0o600) -> Path:
    run = home / "run"
    run.mkdir(parents=True, exist_ok=True)
    path = release_context_path(home, context.target_sha)
    path.write_bytes(release_context_bytes(context))
    path.chmod(mode)
    return path


def test_context_roundtrips_through_its_canonical_bytes(tmp_path: Path) -> None:
    home = tmp_path.resolve()
    context = _context(("runner-a", "/ava-a"))
    path = _write_context(home, context)

    read, digest = read_release_context(home, TARGET_SHA)

    assert read == context
    assert digest == hashlib.sha256(path.read_bytes()).hexdigest()
    assert release_context_bytes(context).endswith(b"\n")


def test_missing_context_is_a_named_refusal(tmp_path: Path) -> None:
    with pytest.raises(ReleaseRejectedError, match="no sealed release context"):
        read_release_context(tmp_path.resolve(), TARGET_SHA)


@pytest.mark.parametrize("mutation", ["version", "coverage", "unsorted-names"])
def test_invalid_context_shapes_refuse(tmp_path: Path, mutation: str) -> None:
    home = tmp_path.resolve()
    wire = json.loads(release_context_bytes(_context(("runner-a", "/ava-a"))))
    if mutation == "version":
        wire["version"] = 2
    elif mutation == "coverage":
        wire["schema_digest"] = "9" * 64
    else:
        wire["applied_names"] = ["two", "one"]
    path = release_context_path(home, TARGET_SHA)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes((json.dumps(wire, sort_keys=True, separators=(",", ":")) + "\n").encode())
    path.chmod(0o600)

    with pytest.raises(ReleaseRejectedError, match="does not validate as version 1"):
        read_release_context(home, TARGET_SHA)


def test_context_for_another_target_refuses(tmp_path: Path) -> None:
    home = tmp_path.resolve()
    context = _context(("runner-a", "/ava-a"))
    run = home / "run"
    run.mkdir(parents=True)
    path = release_context_path(home, "1" * 40)
    path.write_bytes(release_context_bytes(context))
    path.chmod(0o600)

    with pytest.raises(ReleaseRejectedError, match="different rollout target"):
        read_release_context(home, "1" * 40)


def test_context_outside_its_private_run_file_refuses(tmp_path: Path) -> None:
    home = tmp_path.resolve()
    path = _write_context(home, _context(("runner-a", "/ava-a")))
    path.chmod(0o644)

    with pytest.raises(ReleaseRejectedError, match="canonical private run file"):
        read_release_context(home, TARGET_SHA)


def test_non_canonical_context_bytes_refuse(tmp_path: Path) -> None:
    home = tmp_path.resolve()
    context = _context(("runner-a", "/ava-a"))
    path = release_context_path(home, TARGET_SHA)
    path.parent.mkdir(parents=True)
    raw = release_context_bytes(context).replace(b",", b", ", 1)
    assert json.loads(raw)  # parses fine; the agreed canonical form is the gate
    path.write_bytes(raw)
    path.chmod(0o600)

    with pytest.raises(ReleaseRejectedError, match="canonical form"):
        read_release_context(home, TARGET_SHA)


# ── the candidate digest: one derivation point (Q3) ─────────────────────────


def test_candidate_digest_matches_the_published_example_vector() -> None:
    """Q3: the exact canonical bytes and digest later collection consumes."""
    units = [
        _published("runner-b", "/ava-b", artifact="3" * 64, manifest="4" * 64),
        _published("runner-a", "/ava-a", artifact="1" * 64, manifest="2" * 64),
    ]
    canonical = (
        '[["runner-a","' + "1" * 64 + '","' + "2" * 64 + '"],'
        '["runner-b","' + "3" * 64 + '","' + "4" * 64 + '"]]\n'
    )

    assert derive_candidate_digest(units) == hashlib.sha256(canonical.encode()).hexdigest()
    assert (
        derive_candidate_digest(units)
        == "ca020a28801e758aaa1dbbf5037fe7ab7dfafbe5f89c1182f23e3895e3a7b8b9"
    )


def test_candidate_digest_ignores_the_input_order() -> None:
    first = _published("runner-a", "/ava-a", artifact="1" * 64, manifest="2" * 64)
    second = _published("runner-b", "/ava-b", artifact="3" * 64, manifest="4" * 64)

    assert derive_candidate_digest([first, second]) == derive_candidate_digest([second, first])


# ── begin_valid_until (V) ────────────────────────────────────────────────────


def test_begin_valid_until_takes_the_minimum_once() -> None:
    now = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)

    assert begin_valid_until(lease_expires_in_s=600, window_seconds=7200, now=now) == now + (
        timedelta(seconds=600)
    )
    assert begin_valid_until(lease_expires_in_s=9000, window_seconds=7200, now=now) == now + (
        timedelta(seconds=7200)
    )


# ── sealing ──────────────────────────────────────────────────────────────────


def test_seal_assembles_the_plan_bindings_and_digests() -> None:
    facts = [_facts("runner-a", "/ava-a"), _facts("runner-b", "/ava-b")]
    context = _context(("runner-a", "/ava-a"), ("runner-b", "/ava-b"))

    sealed = _seal(facts, context)

    assert sealed.payload.endswith(b"\n")
    assert json.loads(sealed.payload)["target_sha"] == TARGET_SHA
    assert sealed.digest == hashlib.sha256(sealed.payload).hexdigest()
    plan = sealed.plan
    assert [item.unit.machine for item in plan.units] == ["runner-a", "runner-b"]
    assert plan.coordinator.machine == "runner-a"
    assert plan.valid_until == VALID_UNTIL
    assert plan.normal.applied_names == ("one", "two")
    assert plan.units[0].recovery.artifact_digest == RECOVERY_ARTIFACT
    assert plan.units[0].recovery_schema_digest == RECOVERY_SCHEMA
    assert sealed.candidate_digest == derive_candidate_digest(
        [published_unit(fact.publication) for fact in facts]
    )


def test_seal_refuses_without_a_normal_plan_on_every_unit() -> None:
    facts = [_facts("runner-a", "/ava-a"), _facts("runner-b", "/ava-b", candidate=False)]
    context = _context(("runner-a", "/ava-a"), ("runner-b", "/ava-b"))

    with pytest.raises(ManagedWriterBarrierError, match="normal start plan on every unit"):
        _seal(facts, context)


def test_seal_refuses_facts_that_do_not_match_the_context() -> None:
    facts = [_facts("runner-a", "/ava-a"), _facts("runner-b", "/ava-b")]
    context = _context(("runner-a", "/ava-a"), ("runner-b", "/ava-other"))

    with pytest.raises(ManagedWriterBarrierError, match="do not match"):
        _seal(facts, context)


def test_seal_refuses_a_coordinator_outside_the_roster() -> None:
    facts = [_facts("runner-a", "/ava-a")]
    context = _context(("runner-a", "/ava-a"))

    with pytest.raises(ManagedWriterBarrierError, match="coordinating unit"):
        seal_operator_plan(
            facts,
            context,
            operation=_operation(),
            valid_until=VALID_UNTIL,
            coordinator_machine="runner-x",
            coordinator_home="/ava-x",
        )


# ── the fan-out roster ───────────────────────────────────────────────────────


def test_build_targets_maps_every_registered_unit_with_its_sealed_refs() -> None:
    context = _context(("runner-a", "/ava-a"), ("runner-b", "/ava-b"))
    registered = {("runner-a", "/ava-a"), ("runner-b", "/ava-b")}

    targets = build_targets(registered, {"runner-a": "http://a", "runner-b": None}, context)

    assert [target.machine for target in targets] == ["runner-a", "runner-b"]
    assert targets[0].candidate == context.units[0].candidate
    assert targets[0].recovery.artifact_digest == RECOVERY_ARTIFACT
    assert targets[0].recovery.schema_digest == RECOVERY_SCHEMA
    assert targets[0].ops_url == "http://a"


def test_build_targets_refuses_a_context_that_does_not_cover_the_roster() -> None:
    context = _context(("runner-a", "/ava-a"))

    with pytest.raises(ManagedWriterBarrierError, match="does not cover the registered units"):
        build_targets({("runner-a", "/ava-a"), ("runner-b", "/ava-b")}, {}, context)


def test_build_targets_refuses_two_units_of_one_machine() -> None:
    context = _context(("runner-a", "/ava-a"))

    with pytest.raises(ManagedWriterBarrierError, match="one registered unit per machine"):
        build_targets({("runner-a", "/ava-a"), ("runner-a", "/ava-two")}, {}, context)


# ── the dispatch gate ────────────────────────────────────────────────────────


def _world() -> tuple[Any, list[PreparedFactTarget], dict[str, tuple[str, str]]]:
    facts = [_facts("runner-a", "/ava-a"), _facts("runner-b", "/ava-b")]
    context = _context(("runner-a", "/ava-a"), ("runner-b", "/ava-b"))
    sealed = _seal(facts, context)
    targets = build_targets({("runner-a", "/ava-a"), ("runner-b", "/ava-b")}, {}, context)
    return sealed, targets, _ack_bindings(facts)


def _ack(
    machine: str,
    home: str,
    *,
    plan_digest: str,
    receipt_digest: str | None = None,
    refusal: str | None = None,
) -> dict[str, Any]:
    return PrepareDispatchResult(
        machine=machine,
        home=home,
        plan_digest=plan_digest,
        receipt_digest=receipt_digest,
        refusal=refusal,
    ).model_dump(mode="json")


def _stub_dispatch(
    monkeypatch: pytest.MonkeyPatch, answers: dict[str, Any]
) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    async def dispatch(
        target_machine: str, kind: str, payload: dict[str, Any], **kwargs: Any
    ) -> dict[str, Any]:
        calls.append({"target": target_machine, "kind": kind, "payload": payload, "kwargs": kwargs})
        answer = answers[target_machine]
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr("ops.cluster_rpc.dispatch_to_machine", dispatch)
    return calls


def test_dispatch_gate_opens_on_the_full_acknowledged_roster(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    sealed, targets, expected = _world()
    answers = {
        machine: _ack(machine, home, plan_digest=sealed.digest, receipt_digest=receipt)
        for machine, (home, receipt) in expected.items()
    }
    calls = _stub_dispatch(monkeypatch, answers)

    dispatch_prepared_plan(sealed, targets, expected=expected)

    assert [call["kind"] for call in calls] == ["cluster_prepare_dispatch"] * 2
    assert calls[0]["payload"] == {
        "plan_json": sealed.payload.decode("ascii"),
        "artifact_digest": ARTIFACT,
    }
    assert calls[0]["target"] == "runner-a"
    out = capsys.readouterr().out
    assert "runner-a: prepared plan acknowledged" in out
    assert "runner-b: prepared plan acknowledged" in out


def test_dispatch_gate_refuses_when_one_unit_refuses(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    sealed, targets, expected = _world()
    home_a, receipt_a = expected["runner-a"]
    home_b, _receipt_b = expected["runner-b"]
    _stub_dispatch(
        monkeypatch,
        {
            "runner-a": _ack(
                "runner-a", home_a, plan_digest=sealed.digest, receipt_digest=receipt_a
            ),
            "runner-b": _ack(
                "runner-b",
                home_b,
                plan_digest=sealed.digest,
                refusal="candidate image missing",
            ),
        },
    )

    with pytest.raises(ManagedWriterBarrierError, match="1 of 2 units did not acknowledge"):
        dispatch_prepared_plan(sealed, targets, expected=expected)

    assert "runner-b: refused: candidate image missing" in capsys.readouterr().err


def _drift_plan_digest(ack: dict[str, Any]) -> None:
    ack.update(plan_digest="9" * 64)


def _drift_receipt_digest(ack: dict[str, Any]) -> None:
    ack.update(receipt_digest="9" * 64)


def _drift_machine(ack: dict[str, Any]) -> None:
    ack.update(machine="runner-x")


def _drift_home(ack: dict[str, Any]) -> None:
    ack.update(home="/ava-x")


@pytest.mark.parametrize(
    ("mutate", "detail"),
    [
        (_drift_plan_digest, "validated a different sealed plan"),
        (_drift_receipt_digest, "different prepared receipt"),
        (_drift_machine, "another machine"),
        (_drift_home, "another unit home"),
    ],
)
def test_dispatch_gate_refuses_every_ack_binding_drift(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    mutate: Callable[[dict[str, Any]], None],
    detail: str,
) -> None:
    sealed, targets, expected = _world()
    home_a, receipt_a = expected["runner-a"]
    home_b, receipt_b = expected["runner-b"]
    ack_a = _ack("runner-a", home_a, plan_digest=sealed.digest, receipt_digest=receipt_a)
    ack_b = _ack("runner-b", home_b, plan_digest=sealed.digest, receipt_digest=receipt_b)
    mutate(ack_b)
    _stub_dispatch(monkeypatch, {"runner-a": ack_a, "runner-b": ack_b})

    with pytest.raises(ManagedWriterBarrierError, match="1 of 2 units did not acknowledge"):
        dispatch_prepared_plan(sealed, targets, expected=expected)

    assert detail in capsys.readouterr().err


def test_dispatch_gate_counts_unreachable_and_failed_ops(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    sealed, targets, expected = _world()
    _stub_dispatch(
        monkeypatch,
        {
            "runner-a": ClusterOpUnreachable("offline"),
            "runner-b": ClusterOpFailed({"error": "refused", "detail": "no image"}),
        },
    )

    with pytest.raises(ManagedWriterBarrierError, match="2 of 2 units did not acknowledge"):
        dispatch_prepared_plan(sealed, targets, expected=expected)

    err = capsys.readouterr().err
    assert "runner-a: unreachable: offline" in err
    assert "runner-b: op failed:" in err


# ── the begin chain (orchestration) ──────────────────────────────────────────


class _FakeTransaction:
    def __enter__(self) -> object:
        return object()

    def __exit__(self, *_exc: object) -> bool:
        return False


def _lease() -> Any:
    from shared.cluster_lock import DeployLease

    return DeployLease(
        holder="gateway:pid77",
        held_for_s=0,
        expires_in_s=600,
        kind="rollout",
        acquired_at=datetime(2026, 9, 20, tzinfo=UTC),
    )


def _chain_world(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    lease: Any = None,
    gather: Any = None,
    registration: JournaledRegistration | None = None,
) -> dict[str, Any]:
    """The begin chain's collaborating stubs around one real context file.

    `registration` is the journal registration the begin's N3 read adopts, or
    None for the first run's empty registration (no database is dialed).
    """
    from cli.commands import _managed_writer_collector as collector_mod
    from cli.commands import _managed_writer_dispatch as dispatch_mod

    def read_registration(_operation: RolloutIdentity) -> JournaledRegistration:
        if registration is not None:
            return registration
        return JournaledRegistration(valid_until=None, plan_digest=None)

    monkeypatch.setattr(collector_mod, "read_journaled_registration", read_registration)

    home = tmp_path.resolve()
    context = _context(("runner-a", str(home)), ("runner-b", "/ava-b"))
    _write_context(home, context)
    facts = [_facts("runner-a", str(home)), _facts("runner-b", "/ava-b")]
    registered = {("runner-a", str(home)), ("runner-b", "/ava-b")}

    monkeypatch.setattr(dispatch_mod, "read_update_lease", lease or _lease)
    monkeypatch.setattr(dispatch_mod, "self_holder", lambda: "gateway:pid77")
    monkeypatch.setattr(dispatch_mod.settings.general, "ava_home", home)
    monkeypatch.setattr(dispatch_mod, "machine_name", lambda: "runner-a")
    monkeypatch.setattr(
        dispatch_mod,
        "_registered_units_with_urls",
        lambda: (registered, {"runner-a": None, "runner-b": None}),
    )
    gathered: list[dict[str, Any]] = []

    def default_gather(targets: Any, *, operation: Any, registered: Any) -> Any:
        gathered.append({"targets": targets, "operation": operation, "registered": registered})
        return facts

    monkeypatch.setattr(dispatch_mod, "gather_prepared_facts", gather or default_gather)
    recorded: list[dict[str, Any]] = []

    def open_pending(conn: Any, publications: Any, **kwargs: Any) -> Any:
        recorded.append({"conn": conn, "publications": publications, **kwargs})
        return build_pending_publication(
            list(publications),
            operation=kwargs["operation"],
            predecessor=None,
            candidate_digest=kwargs["candidate_digest"],
            challenge=JOURNAL_CHALLENGE,
            schema_digest=kwargs["schema_digest"],
            applied_names=kwargs["applied_names"],
            valid_until=kwargs["valid_until"],
            plan_digest=kwargs["plan_digest"],
        )

    monkeypatch.setattr(dispatch_mod, "open_pending_publication", open_pending)
    monkeypatch.setattr(dispatch_mod, "write_transaction", _FakeTransaction)
    dispatched: list[dict[str, Any]] = []

    def dispatch_sealed(
        sealed: Any, targets: Any, *, expected: Any, projections: Any = None
    ) -> None:
        dispatched.append(
            {
                "sealed": sealed,
                "targets": targets,
                "expected": expected,
                "projections": projections,
            }
        )

    monkeypatch.setattr(dispatch_mod, "dispatch_prepared_plan", dispatch_sealed)
    return {
        "module": dispatch_mod,
        "facts": facts,
        "registered": registered,
        "gathered": gathered,
        "recorded": recorded,
        "dispatched": dispatched,
    }


def test_begin_chain_reads_the_context_opens_the_journal_and_dispatches(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    world = _chain_world(monkeypatch, tmp_path)
    before = datetime.now(UTC)

    phase_input = world["module"].begin_managed_writer_publication(TARGET_SHA)

    facts, registered = world["facts"], world["registered"]
    gathered, recorded, dispatched = world["gathered"], world["recorded"], world["dispatched"]
    assert gathered[0]["operation"].target_sha == TARGET_SHA
    assert gathered[0]["operation"].holder == "gateway:pid77"
    assert gathered[0]["registered"] == registered
    assert [target.machine for target in gathered[0]["targets"]] == ["runner-a", "runner-b"]
    assert recorded[0]["publications"] == [fact.publication for fact in facts]
    assert recorded[0]["operation"] == gathered[0]["operation"]
    assert recorded[0]["schema_digest"] == SCHEMA
    assert recorded[0]["applied_names"] == ("one", "two")
    sealed = dispatched[0]["sealed"]
    assert recorded[0]["candidate_digest"] == sealed.candidate_digest
    # F1: the journal registers the begin execution's V and the sealed digest.
    assert recorded[0]["valid_until"] == sealed.plan.valid_until
    assert recorded[0]["plan_digest"] == sealed.digest
    assert [target.machine for target in dispatched[0]["targets"]] == ["runner-a", "runner-b"]
    assert dispatched[0]["expected"] == _ack_bindings(facts)
    assert before + timedelta(seconds=600) <= sealed.plan.valid_until
    assert sealed.plan.valid_until <= datetime.now(UTC) + timedelta(seconds=600)
    # Channel C rides the same execution: the returned hop plans are the ones
    # whose projections were staged by the dispatch.
    plans = list(phase_input.hop_plans)
    assert [plan.machine for plan in plans] == ["runner-a", "runner-b"]
    assert dispatched[0]["projections"] == {plan.machine: plan.projections for plan in plans}
    # Channel D rides it too: the collector input carries the exact staged/shipped
    # bytes and the sealed identities, in the same sealed order.
    collector = phase_input.collector
    assert collector.operation == recorded[0]["operation"]
    assert collector.challenge == JOURNAL_CHALLENGE
    assert collector.valid_until == sealed.plan.valid_until
    assert collector.candidate_digest == sealed.candidate_digest
    assert [unit.machine for unit in collector.units] == ["runner-a", "runner-b"]
    unit = collector.units[0]
    candidate_projection, request_projection = plans[0].projections
    assert (unit.machine, unit.home, unit.ops_url) == ("runner-a", plans[0].home, plans[0].ops_url)
    assert unit.candidate_context == candidate_projection.content.encode("ascii")
    assert unit.request == request_projection.content.encode("ascii")
    assert unit.recovery_context == facts[0].hop_material.recovery_context.encode("ascii")
    assert unit.prepared_receipt_digest == facts[0].publication.prepared_receipt_digest
    out = capsys.readouterr().out
    assert "(read once)" in out
    assert "journal open; all 2 units" in out
    assert str(JOURNAL_CHALLENGE) in out
    assert "adopts the journaled window" not in out, "a first run registers its own window"


def test_begin_chain_adopts_the_journaled_window_on_a_retry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """N3: a same-operation retry re-seals the journaled V, and says so."""
    from cli.commands._managed_writer_collector import JournaledRegistration

    world = _chain_world(
        monkeypatch,
        tmp_path,
        registration=JournaledRegistration(valid_until=JOURNALED_V, plan_digest="f" * 64),
    )

    phase_input = world["module"].begin_managed_writer_publication(TARGET_SHA)

    sealed = world["dispatched"][0]["sealed"]
    assert sealed.plan.valid_until == JOURNALED_V
    assert phase_input.collector.valid_until == JOURNALED_V
    assert world["recorded"][0]["valid_until"] == JOURNALED_V
    out = capsys.readouterr().out
    assert "adopts the journaled window" in out
    assert JOURNALED_V.isoformat() in out
    assert "cannot slide" in out


def test_begin_chain_binds_the_policy_window_as_the_smaller_bound(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The chain reads the configured window: a long lease does not pass its cap."""
    long_lease = replace(_lease(), expires_in_s=9000)
    world = _chain_world(monkeypatch, tmp_path, lease=lambda: long_lease)
    before = datetime.now(UTC)

    world["module"].begin_managed_writer_publication(TARGET_SHA)

    sealed = world["dispatched"][0]["sealed"]
    assert before + timedelta(seconds=7200) <= sealed.plan.valid_until
    assert sealed.plan.valid_until <= datetime.now(UTC) + timedelta(seconds=7200)


def test_begin_chain_refuses_a_lease_the_process_does_not_own(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cli.commands import _managed_writer_dispatch as dispatch_mod

    other = replace(_lease(), holder="gateway:other")
    monkeypatch.setattr(dispatch_mod, "read_update_lease", lambda: other)
    monkeypatch.setattr(dispatch_mod, "self_holder", lambda: "gateway:pid77")

    with pytest.raises(ManagedWriterBarrierError, match="does not own the live rollout lease"):
        dispatch_mod.begin_managed_writer_publication(TARGET_SHA)


def test_begin_chain_refuses_a_lease_of_another_kind(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only a rollout lease may open the managed-writer journal (the seat's own rule)."""
    from cli.commands import _managed_writer_dispatch as dispatch_mod

    restart = replace(_lease(), kind="restart")
    monkeypatch.setattr(dispatch_mod, "read_update_lease", lambda: restart)
    monkeypatch.setattr(dispatch_mod, "self_holder", lambda: "gateway:pid77")

    with pytest.raises(ManagedWriterBarrierError, match="does not own the live rollout lease"):
        dispatch_mod.begin_managed_writer_publication(TARGET_SHA)


def test_begin_chain_requires_the_pinned_target(monkeypatch: pytest.MonkeyPatch) -> None:
    from cli.commands import _managed_writer_dispatch as dispatch_mod

    with pytest.raises(ManagedWriterBarrierError, match="requires the rollout's pinned target"):
        dispatch_mod.begin_managed_writer_publication(None)


@pytest.mark.parametrize(
    "error",
    [
        ClusterOpFailed({"error": "prepared facts refused"}),
        ClusterOpUnreachable("offline"),
    ],
)
def test_begin_chain_wraps_a_gather_refusal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, error: Exception
) -> None:
    def gather(*_args: Any, **_kwargs: Any) -> Any:
        raise error

    world = _chain_world(monkeypatch, tmp_path, gather=gather)

    with pytest.raises(ManagedWriterBarrierError, match="gather refused"):
        world["module"].begin_managed_writer_publication(TARGET_SHA)


def test_registered_units_with_urls_reads_the_roster_and_urls_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The roster read is one short transaction: locked registered units plus one
    machines scan; a missing URL row still yields the target (the dial refuses)."""
    from cli.commands import _managed_writer_dispatch as dispatch_mod

    class _Cursor:
        def fetchall(self) -> list[tuple[str, str | None]]:
            return [("runner-a", "http://a"), ("runner-b", None)]

    class _Connection:
        def __init__(self) -> None:
            self.statements: list[str] = []

        def execute(self, sql: str) -> _Cursor:
            self.statements.append(sql)
            return _Cursor()

    class _Transaction:
        def __enter__(self) -> _Connection:
            return conn

        def __exit__(self, *_exc: object) -> bool:
            return False

    def _lock(_conn: Any) -> set[tuple[str, str]]:
        return {("runner-a", "/ava-a")}

    conn = _Connection()
    monkeypatch.setattr(dispatch_mod, "write_transaction", _Transaction)
    monkeypatch.setattr(dispatch_mod, "lock_registered_units", _lock)

    registered, urls = dispatch_mod._registered_units_with_urls()

    assert registered == {("runner-a", "/ava-a")}
    assert urls == {"runner-a": "http://a", "runner-b": None}
    assert len(conn.statements) == 1
    assert "machines" in conn.statements[0]
    assert "gateway_url" in conn.statements[0]


# ── the phase-input assembly (channels C+D, task #4129 I4/I5) ──────────────


def _hop_world() -> tuple[list[PreparedUnitFacts], list[PreparedFactTarget], RolloutIdentity]:
    facts = [_facts("runner-a", "/ava-a"), _facts("runner-b", "/ava-b")]
    context = _context(("runner-a", "/ava-a"), ("runner-b", "/ava-b"))
    targets = build_targets({("runner-a", "/ava-a"), ("runner-b", "/ava-b")}, {}, context)
    return facts, targets, _operation()


def test_assemble_phase_inputs_binds_each_projection_to_its_content_name() -> None:
    facts, targets, operation = _hop_world()
    challenge = UUID(int=11)

    phase_input = assemble_phase_inputs(
        facts,
        targets,
        operation=operation,
        valid_until=VALID_UNTIL,
        challenge=challenge,
        schema_digest=SCHEMA,
        candidate_digest=CANDIDATE_DIGEST,
    )

    plans = list(phase_input.hop_plans)
    assert [plan.machine for plan in plans] == ["runner-a", "runner-b"]
    plan = plans[0]
    assert (plan.home, plan.ops_url, plan.artifact_digest) == ("/ava-a", None, ARTIFACT)
    candidate_projection, request_projection = plan.projections
    assert candidate_projection.name == prepared_hop_name(
        "candidate-context", candidate_projection.content
    )
    assert request_projection.name == prepared_hop_name("request", request_projection.content)
    assert plan.request_path == f"/ava-a/run/{request_projection.name}"

    context = PreparedObservation.model_validate_json(candidate_projection.content)
    assert context.operation == operation
    assert context.challenge.challenge == challenge
    assert context.challenge.valid_until == VALID_UNTIL
    assert context.schema_digest == SCHEMA
    assert context.expected == facts[0].publication.receipt.expected

    request = BootstrapHopRequest.model_validate_json(request_projection.content)
    assert request.candidate_context == f"/ava-a/run/{candidate_projection.name}"
    assert request.recovery_context == facts[0].hop_material.recovery_context_path
    assert request.inventory_receipt == (
        f"/ava-a/run/release-inventory-{facts[0].publication.prepared_receipt_digest}.json"
    )
    assert request.predecessor == facts[0].hop_material.predecessor
    assert request.normal_release_path is None

    # The collector input is derived in the same loop: exact bytes, sealed
    # identities, and the same (machine, home) order as the plans.
    collector = phase_input.collector
    assert collector.operation == operation
    assert collector.challenge == challenge
    assert collector.valid_until == VALID_UNTIL
    assert collector.candidate_digest == CANDIDATE_DIGEST
    assert [unit.machine for unit in collector.units] == ["runner-a", "runner-b"]
    unit = collector.units[0]
    assert (unit.machine, unit.home, unit.ops_url) == ("runner-a", "/ava-a", None)
    assert unit.candidate_context == candidate_projection.content.encode("ascii")
    assert unit.request == request_projection.content.encode("ascii")
    assert unit.recovery_context == facts[0].hop_material.recovery_context.encode("ascii")
    assert unit.prepared_receipt_digest == facts[0].publication.prepared_receipt_digest


def test_dispatch_carries_each_units_projections_only(monkeypatch: pytest.MonkeyPatch) -> None:
    sealed, targets, expected = _world()
    answers = {
        machine: _ack(machine, home, plan_digest=sealed.digest, receipt_digest=receipt)
        for machine, (home, receipt) in expected.items()
    }
    calls = _stub_dispatch(monkeypatch, answers)
    projection = ProjectionFile(name=prepared_hop_name("request", "{}\n"), content="{}\n")

    dispatch_prepared_plan(
        sealed, targets, expected=expected, projections={"runner-a": (projection,)}
    )

    assert calls[0]["payload"]["projections"] == [projection.model_dump(mode="json")]
    assert "projections" not in calls[1]["payload"]
