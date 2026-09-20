"""Coordinator-side gathering of per-unit prepared facts (task #4129, channel A).

The pull half of the `cluster_prepare_facts` op: dispatch to every target
unit, re-verify each shipment from its bytes — never trusting a shipment
claim — and return the begin seat's exact input plus the hop-projection
material. The transport wrapper follows `cli.commands._update_fanout`'s shape
(a sync CLI-callable wrapper over one `asyncio.gather`), so a slow unit cannot
serialize the gather behind its neighbours — but unlike the fanout's per-target
containment (`(name, status, detail)` for every host), this gather is fail-fast:
a refusal or an unreachable host propagates to the caller's refusal ladder.

Every refusal is a `ManagedWriterBarrierError`; nothing is written here, and
nothing derived here is authority — the seats revalidate under their locks.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass

from cli.commands._update_publication import PreparedUnitPublication
from ops.rpc_prepare_facts import (
    ImageRef,
    PrepareFactsPayload,
    PrepareFactsResult,
    RestrictedHopMaterial,
)
from shared.managed_writer_barrier import ManagedWriterBarrierError, RolloutIdentity
from shared.managed_writer_publication import PublishedUnit
from shared.runtime_publication_input import PreparationReceipt, _receipt_expected


@dataclass(frozen=True)
class PreparedUnitFacts:
    """One unit's verified shipment: begin-seat input plus hop material."""

    publication: PreparedUnitPublication
    previous_selector: str | None
    recovery: ImageRef
    hop_material: RestrictedHopMaterial


@dataclass(frozen=True)
class PreparedFactTarget:
    """One unit's dispatch endpoint plus the coordinator's sealed image refs.

    Candidate and recovery images are rendered per unit -- the release manifest
    binds its rendering host's platform (`shared/runtime_prepare`), and the
    operator plan carries each unit's digests -- so the refs travel per target;
    a uniform fleet simply repeats them.
    """

    machine: str
    ops_url: str | None
    candidate: ImageRef
    recovery: ImageRef


def _validated_hop_material(
    material: RestrictedHopMaterial, *, unit: PublishedUnit
) -> RestrictedHopMaterial:
    """Shape-and-consistency checks on the shipped hop material (read-once evidence).

    The material's authority is re-derived on the unit by the hop itself (the
    ``ava-ops`` command line compared argument by argument, the observer's
    challenge response); the coordinator refuses here only the shapes the
    begin chain must not turn into a dispatched hop request -- that would be
    caught by the updater, but after the gate and off the unit that shipped it.
    """
    from pathlib import PurePosixPath

    from services.agent_ops.bootstrap import PreparedObservation

    if not material.recovery_context.isascii():
        raise ManagedWriterBarrierError("restricted-A context shipment is not ASCII text")
    raw = material.recovery_context.encode("ascii")
    if len(raw) > 64 * 1024:
        raise ManagedWriterBarrierError("restricted-A context shipment exceeds its bound")
    try:
        context = PreparedObservation.model_validate_json(raw)
    except ValueError as exc:
        raise ManagedWriterBarrierError(
            "restricted-A context shipment is not a prepared observation"
        ) from exc
    if (
        context.expected.machine != unit.machine
        or context.expected.home != unit.home
        or context.expected.artifact_digest == unit.artifact_digest
    ):
        raise ManagedWriterBarrierError(
            "restricted-A context shipment does not describe the unit's predecessor image"
        )
    path = PurePosixPath(material.recovery_context_path)
    if (
        not path.is_absolute()
        or ".." in path.parts
        or str(path) != material.recovery_context_path
        or path.parent != PurePosixPath(unit.home) / "run"
    ):
        raise ManagedWriterBarrierError(
            "restricted-A context path is not a canonical private unit reference"
        )
    return material


def validate_prepared_facts(
    result: PrepareFactsResult, *, target: PreparedFactTarget, registered: set[tuple[str, str]]
) -> PreparedUnitFacts:
    """Re-derive every shipment binding from the bytes; refuse on any drift.

    `target` is the sealed dispatch this response must answer: internally
    consistent bytes can still belong to an image the coordinator never sent,
    so the echoed candidate/recovery references are compared against the
    target's, not accepted as the response's own claim (design A-2 item 3).
    """
    unit = result.unit
    if (unit.machine, unit.home) not in registered:
        raise ManagedWriterBarrierError("prepared facts belong to an unregistered unit")
    if not result.receipt_json.isascii():
        raise ManagedWriterBarrierError("prepared receipt shipment is not ASCII text")
    receipt_bytes = result.receipt_json.encode("ascii")
    if hashlib.sha256(receipt_bytes).hexdigest() != unit.prepared_receipt_digest:
        raise ManagedWriterBarrierError(
            "prepared receipt bytes do not match their announced digest"
        )
    expected = _receipt_expected(receipt_bytes)
    if (
        expected.machine,
        expected.home,
        expected.artifact_digest,
        expected.manifest_digest,
        expected.unit().inventory_digest,
    ) != (
        unit.machine,
        unit.home,
        unit.artifact_digest,
        unit.manifest_digest,
        unit.inventory_digest,
    ):
        raise ManagedWriterBarrierError("prepared receipt and shipment unit differ")
    receipt = PreparationReceipt.model_validate_json(receipt_bytes)
    derived = PublishedUnit(
        machine=unit.machine,
        home=unit.home,
        inventory_digest=unit.inventory_digest,
        prepared_receipt_digest=unit.prepared_receipt_digest,
        artifact_digest=unit.artifact_digest,
        manifest_digest=unit.manifest_digest,
    )
    if result.candidate.unit != derived:
        raise ManagedWriterBarrierError("candidate plan belongs to a different prepared unit")
    if result.recovery.artifact_digest == unit.artifact_digest:
        raise ManagedWriterBarrierError("recovery image must differ from the candidate")
    previous_digest = None
    if result.previous_selector is not None:
        if not result.previous_selector.isascii():
            raise ManagedWriterBarrierError("selector predecessor shipment is not ASCII text")
        previous_digest = hashlib.sha256(result.previous_selector.encode("ascii")).hexdigest()
    if result.candidate.previous_selector_digest != previous_digest:
        raise ManagedWriterBarrierError(
            "selector predecessor bytes do not match the candidate plan digest"
        )
    if (unit.artifact_digest, unit.manifest_digest) != (
        target.candidate.artifact_digest,
        target.candidate.manifest_digest,
    ) or result.recovery != target.recovery:
        raise ManagedWriterBarrierError("prepared facts echo images that were not dispatched")
    material = _validated_hop_material(result.hop_material, unit=unit)
    return PreparedUnitFacts(
        publication=PreparedUnitPublication(
            receipt=receipt,
            prepared_receipt_digest=unit.prepared_receipt_digest,
            artifact_digest=unit.artifact_digest,
            manifest_digest=unit.manifest_digest,
            candidate=result.candidate,
        ),
        previous_selector=result.previous_selector,
        recovery=result.recovery,
        hop_material=material,
    )


async def _gather_prepared_facts_async(
    targets: list[PreparedFactTarget],
    *,
    operation: RolloutIdentity,
    registered: set[tuple[str, str]],
    timeout_s: float | None,
) -> tuple[PreparedUnitFacts, ...]:
    # Lazy import (the `_update_fanout` discipline): cli/commands modules load
    # on every `ava` invocation; the ops RPC stack should not.
    from ops import cluster_rpc

    async def one(target: PreparedFactTarget) -> PreparedUnitFacts:
        payload = PrepareFactsPayload(
            operation=operation, candidate=target.candidate, recovery=target.recovery
        ).model_dump(mode="json")
        result = await cluster_rpc.dispatch_to_machine(
            target_machine=target.machine,
            kind="cluster_prepare_facts",
            payload=payload,
            timeout_s=timeout_s,
            ops_url=target.ops_url,
        )
        # The result crossed the wire as JSON; JSON mode validates the
        # shipment's strict evidence models (lists to tuples, RFC3339 datetimes).
        return validate_prepared_facts(
            PrepareFactsResult.model_validate_json(json.dumps(result)),
            target=target,
            registered=registered,
        )

    gathered = list(await asyncio.gather(*(one(target) for target in targets)))
    keys = [
        (item.publication.receipt.expected.machine, item.publication.receipt.expected.home)
        for item in gathered
    ]
    if len(set(keys)) != len(keys) or set(keys) != registered:
        raise ManagedWriterBarrierError(
            "gathered prepared facts do not cover the registered units exactly"
        )
    return tuple(gathered)


def gather_prepared_facts(
    targets: list[PreparedFactTarget],
    *,
    operation: RolloutIdentity,
    registered: set[tuple[str, str]],
    timeout_s: float | None = None,
) -> tuple[PreparedUnitFacts, ...]:
    """Dispatch `cluster_prepare_facts` to `targets` and verify every shipment.

    `targets` is the rollout's fan-out roster: each unit's machine name,
    pre-resolved ops URL, and the sealed candidate/recovery refs of its image
    render; `registered` is the locked registered-unit set the caller read.
    Business failures (`ClusterOpFailed`) and unreachable hosts
    (`ClusterOpUnreachable`) propagate for the caller's refusal ladder.
    """
    return asyncio.run(
        _gather_prepared_facts_async(
            targets,
            operation=operation,
            registered=registered,
            timeout_s=timeout_s,
        )
    )
