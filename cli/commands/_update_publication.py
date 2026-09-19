"""The publication seats: open the P1 journal, commit the P5 completion.

`begin_pending_publication` (`shared/managed_writer_publication.py`) accepts an
already-assembled `PendingPublication`, but nothing produced one: the sole
callers were tests, and no production component assembled the registered-fleet
plan or minted the single observation challenge the collection step must echo.
`build_pending_publication` derives the journaled unit tuples from per-unit
prepared facts; `open_pending_publication` opens the journal inside the caller's
transaction — the P1 seat for the existing updater's Phase-0 window.

`commit_pending_publication` is the P5 complement for the post-Phase-B window:
once the units recorded their normal-service readbacks, it reads the journaled
set and publishes exactly the complete readbacks through `commit_current`,
leaving the deployment phase and lease with the existing finalizer.

Both seats are inert until the rollout wiring slice connects them: no production
module imports this module, and it performs no filesystem or network work —
prepared facts are expected inventory, not closure or permission. The live-lease
fence, registered unit coverage and the same-operation retry rule remain owned
by `begin_pending_publication`/`lock_rollout`; the seats only supply the plan
and the completion. The caller owns the transaction and the short-lock
discipline: nothing may run while the deployment/registry locks are held, and
delivery is the caller's commit.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import cast
from uuid import UUID, uuid4

import psycopg

from shared.managed_writer_activation import (
    commit_current,
    read_pending_unit_readbacks,
)
from shared.managed_writer_barrier import Digest, ManagedWriterBarrierError, RolloutIdentity
from shared.managed_writer_publication import (
    CandidateUnitPlan,
    NormalStartPlan,
    PendingPublication,
    PublishedUnit,
    _locked_publication,
    begin_pending_publication,
)
from shared.runtime_publication_input import PreparationReceipt


@dataclass(frozen=True)
class PreparedUnitPublication:
    """One unit's verified prepared facts, before any journal entry exists.

    ``receipt`` is the sealed prepared-inventory receipt (``closure="unknown"``,
    expected-only). ``prepared_receipt_digest`` is the sha256 of that receipt's
    exact bytes — the caller must have re-checked it against the sealed file
    (as ``_release_selector.verify_unit_image`` and ``prepared_update._image``
    do); this dataclass re-reads no filesystem. ``artifact_digest`` and
    ``manifest_digest`` name the candidate image those facts were verified
    against. ``candidate`` is the unit's normal-service plan when the release
    includes normal startup, ``None`` otherwise.
    """

    receipt: PreparationReceipt
    prepared_receipt_digest: Digest
    artifact_digest: Digest
    manifest_digest: Digest
    candidate: CandidateUnitPlan | None


def published_unit(facts: PreparedUnitPublication) -> PublishedUnit:
    """Bind one prepared receipt to the immutable unit tuple the journal needs.

    ``inventory_digest`` stays the narrower observer-tuple digest; the full
    receipt's own digest is kept separate because a changed service with no PID
    must not alias. An inconsistent receipt, or a candidate plan whose unit
    differs from the derived one, refuses instead of being journaled.
    """
    expected = facts.receipt.expected
    if facts.receipt.inventory_digest != expected.unit().inventory_digest:
        raise ManagedWriterBarrierError("prepared receipt inventory digest is inconsistent")
    if (facts.artifact_digest, facts.manifest_digest) != (
        expected.artifact_digest,
        expected.manifest_digest,
    ):
        raise ManagedWriterBarrierError("prepared facts and sealed receipt name different images")
    unit = PublishedUnit(
        machine=expected.machine,
        home=expected.home,
        inventory_digest=facts.receipt.inventory_digest,
        prepared_receipt_digest=facts.prepared_receipt_digest,
        artifact_digest=facts.artifact_digest,
        manifest_digest=facts.manifest_digest,
    )
    if facts.candidate is not None and facts.candidate.unit != unit:
        raise ManagedWriterBarrierError("candidate plan belongs to a different prepared unit")
    return unit


def build_pending_publication(
    facts: Sequence[PreparedUnitPublication],
    *,
    operation: RolloutIdentity,
    predecessor: UUID | None,
    candidate_digest: Digest,
    challenge: UUID,
    schema_digest: Digest | None = None,
    applied_names: Sequence[str] = (),
) -> PendingPublication:
    """Assemble the complete-roster journal entry; pure, no database, no effects.

    Units are derived from their prepared receipts and sorted by
    (machine, home) — the order every downstream validator requires. Normal
    startup plans are all-or-none: a plan for one unit is a plan for every unit,
    and it must carry the applied migration SET the updater will later verify.
    ``challenge`` is supplied by the caller because a pure builder cannot know
    whether a same-operation retry must reuse the journaled challenge.
    """
    if not facts:
        raise ManagedWriterBarrierError("a pending publication requires a prepared unit")
    ordered = sorted(
        facts, key=lambda item: (item.receipt.expected.machine, item.receipt.expected.home)
    )
    keys = [(item.receipt.expected.machine, item.receipt.expected.home) for item in ordered]
    if len(keys) != len(set(keys)):
        raise ManagedWriterBarrierError("prepared units are not unique by machine and home")

    units = tuple(published_unit(item) for item in ordered)
    candidates = [item.candidate for item in ordered]
    plan: NormalStartPlan | None = None
    if all(entry is not None for entry in candidates):
        if schema_digest is None:
            raise ManagedWriterBarrierError(
                "a normal start plan requires the applied schema digest"
            )
        names = tuple(applied_names)
        if names != tuple(sorted(set(names))):
            raise ManagedWriterBarrierError("applied migration names must be a sorted set")
        plan = NormalStartPlan(
            schema_digest=schema_digest,
            applied_names=names,
            units=tuple(cast("CandidateUnitPlan", entry) for entry in candidates),
        )
    elif any(entry is not None for entry in candidates):
        raise ManagedWriterBarrierError("normal start plans must cover every prepared unit")
    elif schema_digest is not None or tuple(applied_names) != ():
        raise ManagedWriterBarrierError("normal start plan inputs require candidate plans")

    return PendingPublication(
        operation=operation,
        predecessor=predecessor,
        candidate_digest=candidate_digest,
        challenge=challenge,
        units=units,
        normal_start_plan=plan,
    )


def open_pending_publication(
    conn: psycopg.Connection,
    facts: Sequence[PreparedUnitPublication],
    *,
    operation: RolloutIdentity,
    candidate_digest: Digest,
    schema_digest: Digest | None = None,
    applied_names: Sequence[str] = (),
) -> PendingPublication:
    """Open the pending journal for ``operation`` in the caller's transaction.

    The journal's single observation challenge is minted once: a same-operation
    retry adopts the journaled challenge, because
    ``begin_pending_publication``'s retry check compares the whole entry (a
    fresh challenge would read as a different publication). A pending entry
    from another operation is left for that same check to refuse — recovery is
    explicit, never implicit replacement. The live-lease fence, full registered
    unit coverage, and delivery (the caller's commit) stay with
    ``begin_pending_publication``. Refusals are `ManagedWriterBarrierError`.
    """
    state = _locked_publication(conn)
    existing = state.pending
    challenge = (
        existing.challenge if existing is not None and existing.operation == operation else uuid4()
    )
    pending = build_pending_publication(
        facts,
        operation=operation,
        predecessor=state.current.publication_id if state.current is not None else None,
        candidate_digest=candidate_digest,
        challenge=challenge,
        schema_digest=schema_digest,
        applied_names=applied_names,
    )
    begin_pending_publication(conn, pending)
    return pending


def commit_pending_publication(conn: psycopg.Connection) -> UUID | None:
    """Commit the complete pending publication; skip when there is none.

    The all-unit coordinator step for the existing updater's post-Phase-B
    window: once the units have recorded their normal-service readbacks, the
    coordinator reads the journaled set and publishes exactly the complete
    readbacks through ``commit_current``. Without a pending entry there is
    nothing to commit — a stop-only rollout, or a replayed call after a
    successful commit — and the caller continues to finalization; ordinary
    admission stays deferred until the existing finalizer's release marks the
    phase stable. A partial set refuses (fail-closed): an incomplete activation
    is checked recovery's to clear, never this seat's to publish or drop.

    Requires the caller's transaction (like the journal seat): the journal
    read, the readback revalidation and the commit share one short lock window
    with no filesystem or network work.
    """
    state = _locked_publication(conn)
    pending = state.pending
    if pending is None:
        return None
    readbacks = read_pending_unit_readbacks(conn, pending.operation, pending.challenge)
    if tuple(item.selector.unit for item in readbacks) != pending.units:
        raise ManagedWriterBarrierError(
            "pending publication is not ready to commit: unit readbacks are incomplete"
        )
    return commit_current(conn, pending.operation, pending.challenge, readbacks)
