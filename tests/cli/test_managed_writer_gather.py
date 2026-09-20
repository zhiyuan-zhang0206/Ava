"""`cli.commands._managed_writer_gather` — coordinator-side prepared-facts gathering.

Every shipment is re-derived from the bytes it carries (receipt digest, receipt
to unit binding, candidate plan to unit, predecessor selector digest), and the
gathered set must cover the locked registered roster exactly — a duplicate or a
missing unit refuses instead of reaching the begin seat. The receipts and
candidate plans are constructed directly; their producers are covered by their
own prove scripts.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

import pytest

from cli.commands._managed_writer_gather import (
    PreparedFactTarget,
    gather_prepared_facts,
    validate_prepared_facts,
)
from ops.cluster_rpc import ClusterOpFailed, ClusterOpUnreachable
from ops.rpc_prepare_facts import ImageRef, PrepareFactsResult
from shared.managed_writer_barrier import ManagedWriterBarrierError, RolloutIdentity
from shared.managed_writer_observation import ExpectedUnitWriters
from shared.managed_writer_publication import CandidateUnitPlan, NormalService, PublishedUnit
from shared.runtime_publication_input import PreparationReceipt, PreparedService

ARTIFACT = "a" * 64
MANIFEST = "b" * 64
RECOVERY_ARTIFACT = "d" * 64
RECOVERY_MANIFEST = "e" * 64
RECOVERY_SCHEMA = "f" * 64
TARGET_SHA = "0" * 40


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _receipt(
    machine: str = "runner", home: str = "/ava", *, manifest: str = MANIFEST
) -> PreparationReceipt:
    expected = ExpectedUnitWriters(
        machine=machine,
        home=home,
        artifact_digest=ARTIFACT,
        manifest_digest=manifest,
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


def _shipment(
    machine: str = "runner",
    home: str = "/ava",
    *,
    manifest: str = MANIFEST,
    previous: bytes | None = None,
) -> PrepareFactsResult:
    receipt = _receipt(machine, home, manifest=manifest)
    body = receipt.model_dump_json().encode("ascii")
    expected = receipt.expected
    unit = PublishedUnit(
        machine=machine,
        home=home,
        inventory_digest=expected.unit().inventory_digest,
        prepared_receipt_digest=hashlib.sha256(body).hexdigest(),
        artifact_digest=ARTIFACT,
        manifest_digest=manifest,
    )
    image = f"{home}/releases/{ARTIFACT}"
    selector = {
        "version": 2,
        "artifact_digest": ARTIFACT,
        "manifest_digest": manifest,
        "prepared_receipt_digest": unit.prepared_receipt_digest,
    }
    candidate = CandidateUnitPlan(
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
        previous_selector_digest=(
            hashlib.sha256(previous).hexdigest() if previous is not None else None
        ),
        selector_digest=hashlib.sha256(
            (json.dumps(selector, sort_keys=True, separators=(",", ":")) + "\n").encode()
        ).hexdigest(),
    )
    return PrepareFactsResult(
        unit=unit,
        receipt_json=body.decode("ascii"),
        candidate=candidate,
        recovery=ImageRef(
            artifact_digest=RECOVERY_ARTIFACT,
            manifest_digest=RECOVERY_MANIFEST,
            schema_digest=RECOVERY_SCHEMA,
        ),
        previous_selector=previous.decode("ascii") if previous is not None else None,
    )


def test_validate_rebinds_every_shipment_fact_from_bytes() -> None:
    facts = validate_prepared_facts(
        _shipment(), target=_target("runner"), registered={("runner", "/ava")}
    )

    assert facts.publication.receipt == _receipt()
    assert facts.publication.prepared_receipt_digest == _shipment().unit.prepared_receipt_digest
    assert facts.publication.candidate == _shipment().candidate
    assert facts.previous_selector is None
    assert facts.recovery.artifact_digest == RECOVERY_ARTIFACT


def test_validate_passes_the_predecessor_selector_through() -> None:
    previous = b'{"artifact_digest":"' + b"c" * 64 + b'"}\n'
    facts = validate_prepared_facts(
        _shipment(previous=previous), target=_target("runner"), registered={("runner", "/ava")}
    )

    assert facts.previous_selector == previous.decode("ascii")


def test_unregistered_unit_refuses() -> None:
    with pytest.raises(ManagedWriterBarrierError, match="unregistered"):
        validate_prepared_facts(
            _shipment(), target=_target("other"), registered={("other", "/ava")}
        )


def test_non_ascii_receipt_refuses() -> None:
    shipment = _shipment()
    tampered = shipment.model_copy(update={"receipt_json": "caf\u00e9"})

    with pytest.raises(ManagedWriterBarrierError, match="not ASCII text"):
        validate_prepared_facts(tampered, target=_target("runner"), registered={("runner", "/ava")})


def test_receipt_digest_drift_refuses() -> None:
    shipment = _shipment()
    tampered = shipment.model_copy(update={"receipt_json": shipment.receipt_json + " "})

    with pytest.raises(ManagedWriterBarrierError, match="do not match their announced digest"):
        validate_prepared_facts(tampered, target=_target("runner"), registered={("runner", "/ava")})


def test_shipment_unit_diverging_from_receipt_refuses() -> None:
    shipment = _shipment()
    tampered = shipment.model_copy(
        update={"unit": shipment.unit.model_copy(update={"machine": "other"})}
    )

    with pytest.raises(ManagedWriterBarrierError, match="receipt and shipment unit differ"):
        validate_prepared_facts(tampered, target=_target("other"), registered={("other", "/ava")})


def test_candidate_plan_for_a_different_unit_refuses() -> None:
    shipment = _shipment()
    other_unit = shipment.candidate.unit.model_copy(update={"inventory_digest": "7" * 64})
    tampered = shipment.model_copy(
        update={"candidate": shipment.candidate.model_copy(update={"unit": other_unit})}
    )

    with pytest.raises(ManagedWriterBarrierError, match="different prepared unit"):
        validate_prepared_facts(tampered, target=_target("runner"), registered={("runner", "/ava")})


def test_recovery_image_must_differ_from_the_candidate() -> None:
    shipment = _shipment()
    tampered = shipment.model_copy(
        update={"recovery": shipment.recovery.model_copy(update={"artifact_digest": ARTIFACT})}
    )

    with pytest.raises(ManagedWriterBarrierError, match="recovery image must differ"):
        validate_prepared_facts(tampered, target=_target("runner"), registered={("runner", "/ava")})


def test_predecessor_selector_binding_drift_refuses() -> None:
    registered = {("runner", "/ava")}
    bound = _shipment(previous=b'{"artifact_digest":"' + b"c" * 64 + b'"}\n')

    cleared = bound.model_copy(
        update={"candidate": bound.candidate.model_copy(update={"previous_selector_digest": None})}
    )
    with pytest.raises(ManagedWriterBarrierError, match="predecessor"):
        validate_prepared_facts(cleared, target=_target("runner"), registered=registered)

    swapped = bound.model_copy(
        update={"previous_selector": '{"artifact_digest":"' + "d" * 64 + '"}\n'}
    )
    with pytest.raises(ManagedWriterBarrierError, match="predecessor"):
        validate_prepared_facts(swapped, target=_target("runner"), registered=registered)

    clean = _shipment()
    claimed = clean.model_copy(
        update={
            "candidate": clean.candidate.model_copy(
                update={"previous_selector_digest": _digest("ghost")}
            )
        }
    )
    with pytest.raises(ManagedWriterBarrierError, match="predecessor"):
        validate_prepared_facts(claimed, target=_target("runner"), registered=registered)


def test_validate_refuses_a_candidate_image_that_was_not_dispatched() -> None:
    """Internally consistent bytes can still echo an image never sent; the
    response must match the target's sealed candidate reference."""
    shipment = _shipment(manifest="9" * 64)

    with pytest.raises(ManagedWriterBarrierError, match="not dispatched"):
        validate_prepared_facts(shipment, target=_target("runner"), registered={("runner", "/ava")})


def test_validate_refuses_a_recovery_echo_that_was_not_dispatched() -> None:
    shipment = _shipment()
    tampered = shipment.model_copy(
        update={"recovery": shipment.recovery.model_copy(update={"schema_digest": "8" * 64})}
    )

    with pytest.raises(ManagedWriterBarrierError, match="not dispatched"):
        validate_prepared_facts(tampered, target=_target("runner"), registered={("runner", "/ava")})


def _stub_dispatch(
    monkeypatch: pytest.MonkeyPatch, shipments: dict[str, PrepareFactsResult]
) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    async def dispatch(
        target_machine: str, kind: str, payload: dict[str, Any], **kwargs: Any
    ) -> dict[str, Any]:
        calls.append({"target": target_machine, "kind": kind, "payload": payload})
        return shipments[target_machine].model_dump(mode="json")

    monkeypatch.setattr("ops.cluster_rpc.dispatch_to_machine", dispatch)
    return calls


def _operation() -> RolloutIdentity:
    return RolloutIdentity(
        holder="gateway:pid77", acquired_at=datetime.now(UTC), target_sha=TARGET_SHA
    )


def _target(machine: str, *, manifest_digest: str = MANIFEST) -> PreparedFactTarget:
    """One unit's endpoint plus its sealed refs; a distinct manifest pins the
    per-target dispatch of the image references."""
    return PreparedFactTarget(
        machine=machine,
        ops_url=None,
        candidate=ImageRef(
            artifact_digest=ARTIFACT, manifest_digest=manifest_digest, schema_digest="c" * 64
        ),
        recovery=ImageRef(
            artifact_digest=RECOVERY_ARTIFACT,
            manifest_digest=RECOVERY_MANIFEST,
            schema_digest=RECOVERY_SCHEMA,
        ),
    )


def test_gather_dispatches_the_op_and_covers_the_roster_exactly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = _shipment("runner-a", "/ava-a")
    second = _shipment("runner-b", "/ava-b", manifest="1" * 64)
    calls = _stub_dispatch(monkeypatch, {"runner-a": first, "runner-b": second})

    facts = gather_prepared_facts(
        [_target("runner-a"), _target("runner-b", manifest_digest="1" * 64)],
        operation=_operation(),
        registered={("runner-a", "/ava-a"), ("runner-b", "/ava-b")},
    )

    assert [
        (item.publication.receipt.expected.machine, item.publication.receipt.expected.home)
        for item in facts
    ] == [("runner-a", "/ava-a"), ("runner-b", "/ava-b")]
    assert [call["kind"] for call in calls] == [
        "cluster_prepare_facts",
        "cluster_prepare_facts",
    ]
    assert calls[0]["payload"]["candidate"]["artifact_digest"] == ARTIFACT
    assert calls[0]["payload"]["candidate"]["manifest_digest"] == MANIFEST
    assert calls[1]["payload"]["candidate"]["manifest_digest"] == "1" * 64
    assert calls[0]["payload"]["recovery"]["artifact_digest"] == RECOVERY_ARTIFACT
    assert calls[0]["payload"]["operation"]["target_sha"] == TARGET_SHA


def test_gather_refuses_a_roster_the_shipments_do_not_cover(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_dispatch(monkeypatch, {"runner-a": _shipment("runner-a", "/ava-a")})

    with pytest.raises(ManagedWriterBarrierError, match="registered units exactly"):
        gather_prepared_facts(
            [_target("runner-a")],
            operation=_operation(),
            registered={("runner-a", "/ava-a"), ("runner-b", "/ava-b")},
        )


def test_gather_refuses_duplicate_unit_shipments(monkeypatch: pytest.MonkeyPatch) -> None:
    first = _shipment("runner-a", "/ava-a")
    _stub_dispatch(monkeypatch, {"runner-a": first, "runner-b": first})

    with pytest.raises(ManagedWriterBarrierError, match="registered units exactly"):
        gather_prepared_facts(
            [_target("runner-a"), _target("runner-b")],
            operation=_operation(),
            registered={("runner-a", "/ava-a")},
        )


def test_gather_propagates_business_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    async def dispatch(
        target_machine: str, kind: str, payload: dict[str, Any], **kwargs: Any
    ) -> dict[str, Any]:
        raise ClusterOpFailed({"reason": "unit refused"})

    monkeypatch.setattr("ops.cluster_rpc.dispatch_to_machine", dispatch)

    with pytest.raises(ClusterOpFailed):
        gather_prepared_facts(
            [_target("runner-a")],
            operation=_operation(),
            registered={("runner-a", "/ava-a")},
        )


def test_gather_propagates_unreachable_hosts(monkeypatch: pytest.MonkeyPatch) -> None:
    async def dispatch(
        target_machine: str, kind: str, payload: dict[str, Any], **kwargs: Any
    ) -> dict[str, Any]:
        raise ClusterOpUnreachable("offline")

    monkeypatch.setattr("ops.cluster_rpc.dispatch_to_machine", dispatch)

    with pytest.raises(ClusterOpUnreachable):
        gather_prepared_facts(
            [_target("runner-a")],
            operation=_operation(),
            registered={("runner-a", "/ava-a")},
        )
