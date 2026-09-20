"""Coordinator-side seal and dispatch of the all-unit prepared plan (task #4129, channel B).

The begin position's second half. Once channel A's gathered facts exist, this
module derives the fleet's sealed candidate identity (`derive_candidate_digest`,
the single derivation point), assembles and canonicalizes the immutable
`PreparedOperatorPlan` every unit must see byte-identically, opens the P1
journal through the existing seat, and fans the plan out to every unit's
`cluster_prepare_dispatch` op -- requiring a local-validation acknowledgement
from each before the rollout may enter the hop phase.

The window V is taken once per begin execution here (min of the live lease's
remaining time and the configured policy window) and carried by the sealed plan,
so a lease renewal during the rollout cannot slide it -- a re-run begin
recomputes V, and registering V durably with the pending journal is the
collection phase's (task #4129 I4). Everything except the seat call and the
roster read runs outside database transactions (no filesystem or network work
under a lock), and every refusal is fail-closed: the fleet never half-activates.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from cli.commands._managed_writer_gather import (
    PreparedFactTarget,
    PreparedUnitFacts,
    gather_prepared_facts,
)
from cli.commands._release_context import ReleaseContext, read_release_context
from cli.commands._update_publication import open_pending_publication, published_unit
from cli.prepared_update import PreparedOperatorPlan, PreparedOperatorUnit
from ops.rpc_prepare_dispatch import PrepareDispatchResult
from ops.rpc_prepare_facts import ImageRef
from shared.cluster_lock import read_update_lease, self_holder
from shared.config import settings
from shared.db_transaction import write_transaction
from shared.machine import machine_name
from shared.managed_writer_barrier import (
    ManagedWriterBarrierError,
    RolloutIdentity,
    lock_registered_units,
)
from shared.managed_writer_publication import CandidateUnitPlan, NormalStartPlan, PublishedUnit
from shared.runtime_release import ReleaseRejectedError


@dataclass(frozen=True)
class SealedPlan:
    """The immutable plan, its canonical bytes, and the digests binding both."""

    plan: PreparedOperatorPlan
    payload: bytes
    digest: str
    candidate_digest: str


@dataclass(frozen=True)
class DispatchOutcome:
    """One unit's answer to the plan dispatch."""

    machine: str
    ok: bool
    detail: str


def _canonical_bytes(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def derive_candidate_digest(units: Sequence[PublishedUnit]) -> str:
    """The fleet's sealed candidate identity -- the single derivation point (Q3).

    sha256 over the canonical JSON of the sorted (machine, artifact, manifest)
    triples of the gathered units; the begin seal and the later collection
    consume exactly this value, never a second derivation.
    """
    roster = sorted([unit.machine, unit.artifact_digest, unit.manifest_digest] for unit in units)
    return hashlib.sha256(_canonical_bytes(roster)).hexdigest()


def begin_valid_until(
    *, lease_expires_in_s: float, window_seconds: float, now: datetime
) -> datetime:
    """V, taken once at the begin position: min(lease remaining, policy window).

    The sealed plan carries the value and later phases project it, so lease
    renewals and same-operation retries cannot slide the observation window.
    """
    return now + timedelta(seconds=min(lease_expires_in_s, window_seconds))


def build_targets(
    registered: set[tuple[str, str]], urls: dict[str, str | None], context: ReleaseContext
) -> list[PreparedFactTarget]:
    """The fan-out roster: every registered unit once, with its sealed refs.

    The managed-writer roster gate is all-or-nothing over every registered
    unit (design section 6): an excluded machine refuses here rather than
    shrinking the activation. The machines read deliberately skips the fanout's
    three exclusion latches for the same reason.
    """
    machines = sorted({machine for machine, _home in registered})
    if len(machines) != len(registered):
        raise ManagedWriterBarrierError(
            "the managed-writer roster must carry exactly one registered unit per machine"
        )
    by_machine = {unit.machine: unit for unit in context.units}
    if set(by_machine) != set(machines):
        raise ManagedWriterBarrierError(
            "the sealed release context does not cover the registered units exactly"
        )
    return [
        PreparedFactTarget(
            machine=machine,
            ops_url=urls.get(machine),
            candidate=by_machine[machine].candidate,
            recovery=ImageRef(
                artifact_digest=by_machine[machine].recovery.artifact_digest,
                manifest_digest=by_machine[machine].recovery.manifest_digest,
                schema_digest=by_machine[machine].recovery_schema_digest,
            ),
        )
        for machine in machines
    ]


def seal_operator_plan(
    facts: Sequence[PreparedUnitFacts],
    context: ReleaseContext,
    *,
    operation: RolloutIdentity,
    valid_until: datetime,
    coordinator_machine: str,
    coordinator_home: str,
) -> SealedPlan:
    """Assemble the immutable all-unit plan and its canonical bytes (B-1).

    The recovery binding and the fleet baseline come from the sealed release
    context; the unit bindings and normal plans come from the gathered facts.
    A missing candidate plan on any unit refuses: the plan's normal field is
    all-or-none, and a managed-writer rollout publishes a normal release.
    """
    by_machine = {unit.machine: unit for unit in context.units}
    units: list[PreparedOperatorUnit] = []
    candidates: list[CandidateUnitPlan] = []
    published: list[PublishedUnit] = []
    for item in sorted(
        facts,
        key=lambda entry: (
            entry.publication.receipt.expected.machine,
            entry.publication.receipt.expected.home,
        ),
    ):
        publication = item.publication
        machine = publication.receipt.expected.machine
        home = publication.receipt.expected.home
        sealed = by_machine.get(machine)
        if sealed is None or sealed.recovery.home != home:
            raise ManagedWriterBarrierError(
                "prepared facts and the sealed release context do not match"
            )
        if publication.candidate is None:
            raise ManagedWriterBarrierError(
                "a managed-writer rollout requires the normal start plan on every unit"
            )
        unit = published_unit(publication)
        units.append(
            PreparedOperatorUnit(
                unit=unit,
                recovery=sealed.recovery,
                recovery_schema_digest=sealed.recovery_schema_digest,
            )
        )
        candidates.append(publication.candidate)
        published.append(unit)
    coordinator = next(
        (
            unit
            for unit in published
            if (unit.machine, unit.home) == (coordinator_machine, coordinator_home)
        ),
        None,
    )
    if coordinator is None:
        raise ManagedWriterBarrierError(
            "the coordinating unit is not among the registered prepared units"
        )
    plan = PreparedOperatorPlan(
        version=1,
        request_id=uuid4(),
        target_sha=operation.target_sha,
        coordinator=coordinator,
        units=tuple(units),
        valid_until=valid_until,
        normal=NormalStartPlan(
            schema_digest=context.schema_digest,
            applied_names=context.applied_names,
            units=tuple(candidates),
        ),
    )
    payload = _canonical_bytes(plan.model_dump(mode="json"))
    return SealedPlan(
        plan=plan,
        payload=payload,
        digest=hashlib.sha256(payload).hexdigest(),
        candidate_digest=derive_candidate_digest(published),
    )


def _ack_bindings(facts: Sequence[PreparedUnitFacts]) -> dict[str, tuple[str, str]]:
    """Per machine: the expected home and prepared-receipt digest of an acknowledgement."""
    return {
        item.publication.receipt.expected.machine: (
            item.publication.receipt.expected.home,
            item.publication.prepared_receipt_digest,
        )
        for item in facts
    }


def _ack_outcome(
    target: PreparedFactTarget,
    sealed: SealedPlan,
    expected: dict[str, tuple[str, str]],
    result: dict[str, Any],
) -> DispatchOutcome:
    """One acknowledgement, re-derived from the wire values; never trusted as such."""
    ack = PrepareDispatchResult.model_validate_json(json.dumps(result))
    ack_home, receipt = expected[target.machine]
    if ack.machine != target.machine:
        return DispatchOutcome(
            machine=target.machine, ok=False, detail="acknowledgement belongs to another machine"
        )
    if ack.home != ack_home:
        return DispatchOutcome(
            machine=target.machine, ok=False, detail="acknowledgement belongs to another unit home"
        )
    if ack.refusal is not None:
        return DispatchOutcome(machine=target.machine, ok=False, detail=f"refused: {ack.refusal}")
    if ack.plan_digest != sealed.digest:
        return DispatchOutcome(
            machine=target.machine, ok=False, detail="validated a different sealed plan"
        )
    if ack.receipt_digest != receipt:
        return DispatchOutcome(
            machine=target.machine,
            ok=False,
            detail="validated against a different prepared receipt",
        )
    return DispatchOutcome(machine=target.machine, ok=True, detail="prepared plan acknowledged")


async def _dispatch_plan_async(
    sealed: SealedPlan,
    targets: Sequence[PreparedFactTarget],
    expected: dict[str, tuple[str, str]],
    timeout_s: float | None,
) -> list[DispatchOutcome]:
    # Lazy import (the `_update_fanout` discipline): cli/commands modules load
    # on every `ava` invocation; the ops RPC stack should not.
    from ops import cluster_rpc

    async def one(target: PreparedFactTarget) -> DispatchOutcome:
        try:
            result = await cluster_rpc.dispatch_to_machine(
                target_machine=target.machine,
                kind="cluster_prepare_dispatch",
                payload={
                    "plan_json": sealed.payload.decode("ascii"),
                    "artifact_digest": target.candidate.artifact_digest,
                },
                timeout_s=timeout_s,
                ops_url=target.ops_url,
            )
        except cluster_rpc.ClusterOpUnreachable as exc:
            return DispatchOutcome(machine=target.machine, ok=False, detail=f"unreachable: {exc}")
        except cluster_rpc.ClusterOpFailed as exc:
            return DispatchOutcome(
                machine=target.machine, ok=False, detail=f"op failed: {exc.result!r}"
            )
        return _ack_outcome(target, sealed, expected, result)

    return list(await asyncio.gather(*(one(target) for target in targets)))


def dispatch_prepared_plan(
    sealed: SealedPlan,
    targets: Sequence[PreparedFactTarget],
    *,
    expected: dict[str, tuple[str, str]],
    timeout_s: float | None = None,
) -> None:
    """Fan the sealed plan out and enforce the all-unit acknowledgement gate (B-2).

    A unit refuses by acknowledgement, not by exception: every answer (ok or
    refusal) prints one line, and any non-ok answer raises the summary refusal
    after all units have answered, so the operator sees the whole roster's
    verdict at once. On refusal the pending journal stays: the exact pre-stop
    clear is the abort step's (a later slice), never this position's.
    """
    outcomes = asyncio.run(_dispatch_plan_async(sealed, targets, expected, timeout_s))
    refused: list[str] = []
    for outcome in outcomes:
        if outcome.ok:
            print(f"  \u2713 {outcome.machine}: {outcome.detail}")
        else:
            print(f"  \u2717 {outcome.machine}: {outcome.detail}", file=sys.stderr)
            refused.append(outcome.machine)
    if refused:
        raise ManagedWriterBarrierError(
            "the prepared-plan dispatch gate refused: "
            f"{len(refused)} of {len(targets)} units did not acknowledge"
        )


def _registered_units_with_urls() -> tuple[set[tuple[str, str]], dict[str, str | None]]:
    """The registered roster plus each machine's pre-resolved ops URL.

    Follows `cli.commands._update_fanout._list_agent_runners`'s one machines
    read, deliberately without its three exclusion latches: the roster gate
    covers every registered unit, and a machine whose URL row is missing still
    gets a target (the dial then fails loudly instead of shrinking the set).
    """
    with write_transaction() as conn:
        registered = lock_registered_units(conn)
        rows = conn.execute("SELECT name, gateway_url FROM machines ORDER BY name").fetchall()
    return registered, {row[0]: row[1] for row in rows}


def begin_managed_writer_publication(target_sha: str | None) -> None:
    """Run the begin position's full chain; every refusal is a barrier error.

    Raises `ManagedWriterBarrierError` for a refused context/roster/gather/
    seal/dispatch; infrastructure failures (database, an op failure outside the
    ladder) propagate unchanged. On a refusal after the journal opened, the
    pending entry persists for checked recovery -- the exact pre-stop clear is
    the abort step's, not this position's.
    """
    from ops import cluster_rpc

    if target_sha is None:
        raise ManagedWriterBarrierError(
            "the managed-writer begin requires the rollout's pinned target"
        )
    home = settings.general.ava_home
    lease = read_update_lease()
    if (
        lease is None
        or lease.holder != self_holder()
        or lease.note is not None
        or lease.kind != "rollout"
    ):
        raise ManagedWriterBarrierError("the begin position does not own the live rollout lease")
    if lease.acquired_at is None:
        raise ManagedWriterBarrierError("the live rollout lease has no acquired-at identity")
    operation = RolloutIdentity(
        holder=lease.holder, acquired_at=lease.acquired_at, target_sha=target_sha
    )
    valid_until = begin_valid_until(
        lease_expires_in_s=lease.expires_in_s,
        window_seconds=settings.gateway.update_managed_writer_window_seconds,
        now=datetime.now(UTC),
    )
    try:
        context, context_digest = read_release_context(home, target_sha)
    except ReleaseRejectedError as exc:
        raise ManagedWriterBarrierError(str(exc)) from exc
    print(
        f"  \u00b7 managed-writer begin: release context {context_digest[:12]} "
        f"for target {target_sha[:12]} (read once)"
    )
    registered, urls = _registered_units_with_urls()
    targets = build_targets(registered, urls, context)
    try:
        facts = gather_prepared_facts(targets, operation=operation, registered=registered)
    except (cluster_rpc.ClusterOpFailed, cluster_rpc.ClusterOpUnreachable) as exc:
        raise ManagedWriterBarrierError(f"the prepared-facts gather refused: {exc}") from exc
    sealed = seal_operator_plan(
        facts,
        context,
        operation=operation,
        valid_until=valid_until,
        coordinator_machine=machine_name(),
        coordinator_home=str(home),
    )
    print(
        f"  \u00b7 managed-writer begin: sealed plan {sealed.digest[:12]} over {len(facts)} units"
    )
    with write_transaction() as conn:
        open_pending_publication(
            conn,
            [item.publication for item in facts],
            operation=operation,
            candidate_digest=sealed.candidate_digest,
            schema_digest=context.schema_digest,
            applied_names=context.applied_names,
        )
    dispatch_prepared_plan(sealed, targets, expected=_ack_bindings(facts))
    print(
        f"  \u2713 managed-writer begin: journal open; all {len(targets)} units "
        "acknowledged the sealed plan"
    )
