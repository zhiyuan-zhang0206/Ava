"""Operator recovery for an interrupted rollout's durable pending publication.

A rollout that entered the managed-writer publication protocol leaves a durable
`pending` record in `deployment_state.managed_writer_evidence` before it stops
anything. That record deliberately outlives its lease: ordinary acquisition,
generic cluster recovery, settle conversion and release all refuse while it
exists, and every new agent birth defers (the phase-side deferral is lease-scoped
on purpose; this record is durable on purpose). The only way out is the checked
protocol on that row: forward completion publishes `current`, or an explicit
recovery replaces the abandoned operation under a NEW live rollout lease, a NEW
challenge and a FRESH complete writer closure — never a generic clear, never a
hand-edited evidence field.

This module is the explicit recovery's operator seat:

    read state -> prove every live owner gone -> claim the abandoned lease
    (new identity + challenge) -> produce fresh per-unit writer closure ->
    `recover_pending_publication` with the replacement.

The exact pre-stop abort (`pre_stop_abort_pending_publication_op`, task #4129
C-4) is the window's other, simpler exit: while every journaled unit reports
no bootstrap-recovery journal -- no hop child has begun, and the pending
journal records no collection, migration or readbacks -- it clears the
never-effective pending journal and releases the abandoned lease in one
guarded write. Any doubt refuses (a unit that cannot answer, a journal that
exists, a journal that already records effects), and the write that cuts the
window off is also what stops a hop child starting after the proof: its own
admission re-reads the live lease and refuses.

`claim_abandoned_pending_lease` and `complete_pending_publication_recovery` are
the seat's two storage-level steps; `run_pending_publication_recovery` composes
them around a caller-supplied closure collector (the trusted producer is the
rollout's own per-unit observation pipeline; until it is connected,
`pending_publication_recovery_op` refuses BEFORE any mutation rather than accept
operator-supplied or fabricated evidence).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID, uuid4

from pydantic import ValidationError

import shared.db
from ops import cluster_session
from ops.cluster import ClusterUpdateInProgress
from ops.ops_bootstrap_hop import BootstrapRecoveryReadResult
from shared import ui_update_state, updater_handoff
from shared.audit_events import insert_event_log
from shared.cluster_lock import (
    DeployLease,
    holder_process_gone,
    read_update_lease,
    self_holder,
)
from shared.cluster_pending_recovery import (
    abandon_pending_publication_lease,
    claim_pending_recovery_lease,
)
from shared.db_transaction import write_transaction
from shared.host_deploy_state import updater_lease_live
from shared.managed_writer_barrier import (
    ManagedWriterBarrierError,
    ManagedWriterCollection,
    RolloutIdentity,
)
from shared.managed_writer_publication import (
    PendingPublication,
    PublishedUnit,
    WriterPublication,
    _locked_publication,
    recover_pending_publication,
)

_CLOSURE_PRODUCER_NOT_CONNECTED = (
    "pending-publication recovery requires a fresh complete per-unit writer closure "
    "from the rollout's own observation pipeline; that producer is not connected in "
    "this build, so recovery refused before touching the lease rather than accept "
    "operator-supplied or fabricated evidence"
)


@dataclass(frozen=True)
class PendingRecoveryState:
    """Read-only snapshot the recovery seat acts on."""

    pending: bool
    abandoned: RolloutIdentity | None
    abandoned_operation_json: dict[str, Any] | None
    abandoned_units: tuple[PublishedUnit, ...]
    candidate_digest: str | None
    current_publication_id: UUID | None
    lease: DeployLease | None
    stale_holder: str | None
    stale_holder_probed_gone: bool | None
    stale_holder_held_for_s: float | None


@dataclass(frozen=True)
class PendingRecoveryClaim:
    """The replacement operation a fresh closure must be produced for."""

    operation: RolloutIdentity
    challenge: UUID
    abandoned: RolloutIdentity
    abandoned_units: tuple[PublishedUnit, ...]
    candidate_digest: str
    current_publication_id: UUID | None


def read_pending_recovery_state() -> PendingRecoveryState:
    """Read the journal row, the live lease, and any still-recorded stale holder.

    Parses the strict v2 envelope: malformed or unknown-version evidence refuses
    (fail-closed) instead of falling back. The stale-holder probe is evidence for
    a later claim, never permission to act — the takeover CAS re-pins the exact
    journaled operation before anything changes.
    """
    with shared.db.connect(autocommit=True) as conn:
        row = conn.execute(
            "SELECT managed_writer_evidence, holder, acquired_at FROM deployment_state WHERE id = 1"
        ).fetchone()
        if row is None:
            raise ClusterUpdateInProgress(
                "the cluster deploy state row is missing; recovery refused"
            )
        # psycopg row types are opaque; pin them to `Any` so the JSONB parse below
        # is not a cascade of partially-unknown reads.
        evidence: Any = row[0]
        holder: Any = row[1]
        acquired_at: Any = row[2]
        live_lease = read_update_lease(conn=conn)
    if evidence is None:
        return PendingRecoveryState(
            pending=False,
            abandoned=None,
            abandoned_operation_json=None,
            abandoned_units=(),
            candidate_digest=None,
            current_publication_id=None,
            lease=live_lease,
            stale_holder=None,
            stale_holder_probed_gone=None,
            stale_holder_held_for_s=None,
        )
    try:
        publication = WriterPublication.model_validate_json(json.dumps(evidence))
    except ValidationError as exc:
        raise ClusterUpdateInProgress(
            "managed-writer publication evidence is unreadable (fail-closed); recovery refused"
        ) from exc
    current_id = publication.current.publication_id if publication.current is not None else None
    pending = publication.pending
    if pending is None:
        return PendingRecoveryState(
            pending=False,
            abandoned=None,
            abandoned_operation_json=None,
            abandoned_units=(),
            candidate_digest=None,
            current_publication_id=current_id,
            lease=live_lease,
            stale_holder=None,
            stale_holder_probed_gone=None,
            stale_holder_held_for_s=None,
        )
    if not isinstance(evidence, dict):
        raise ClusterUpdateInProgress(
            "managed-writer evidence is not a JSON object (fail-closed); recovery refused"
        )
    pending_raw = cast("dict[str, Any]", evidence).get("pending")
    raw_operation = (
        cast("dict[str, Any]", pending_raw).get("operation")
        if isinstance(pending_raw, dict)
        else None
    )
    if not isinstance(raw_operation, dict):
        raise ClusterUpdateInProgress(
            "the pending publication has no readable operation; recovery refused"
        )
    raw_operation = cast("dict[str, Any]", raw_operation)
    stale_holder: str | None = None
    stale_gone: bool | None = None
    stale_held: float | None = None
    if live_lease is None and holder is not None:
        stale_holder = holder
        stale_held = (
            (datetime.now(UTC) - acquired_at).total_seconds() if acquired_at is not None else None
        )
        stale_gone = holder_process_gone(holder, held_for_s=stale_held)
    return PendingRecoveryState(
        pending=True,
        abandoned=pending.operation,
        abandoned_operation_json=raw_operation,
        abandoned_units=pending.units,
        candidate_digest=pending.candidate_digest,
        current_publication_id=current_id,
        lease=live_lease,
        stale_holder=stale_holder,
        stale_holder_probed_gone=stale_gone,
        stale_holder_held_for_s=stale_held,
    )


def _require_no_live_deploy() -> None:
    """Fail-closed liveness gate, mirroring `cluster_recover_op`'s checks.

    The two manual recovery verbs must refuse on the same conditions — a live
    updater handoff, a live orchestration session, a live host updater lease.
    The deploy lease itself is judged by `_prove_abandoned_holder_gone`, which
    requires positive death evidence rather than mere TTL expiry.
    """
    handoff = updater_handoff.read()
    if handoff.status == "invalid":
        raise ClusterUpdateInProgress(
            "an updater spawn handoff is still active or unreadable — recovery refused until "
            "its child publishes the DB lease or its safety bound expires"
        )
    if handoff.status == "pending" and not handoff.expired:
        raise ClusterUpdateInProgress(
            "an updater child is still inside its protected startup window — recovery refused "
            "until that pending handoff expires"
        )
    if handoff.status == "running" and updater_handoff.owner_is_live(handoff):
        raise ClusterUpdateInProgress(
            "an updater process still owns this host — recovery refused while it is alive"
        )
    if not updater_handoff.allows_generic_recovery(handoff):
        raise ClusterUpdateInProgress(
            "retained updater compensation requires its own checked recovery first — "
            "publication recovery refused"
        )
    live_session = cluster_session.live_orchestration_session()
    if live_session is not None:
        raise ClusterUpdateInProgress(
            f"orchestration session {live_session!r} is still alive — recovery refused; "
            "wait for it to finish or kill it first"
        )
    if updater_lease_live():
        raise ClusterUpdateInProgress(
            "an update is in flight on this host — its updater lease is live; recovery refused"
        )


def _prove_abandoned_holder_gone(state: PendingRecoveryState) -> tuple[str, datetime] | None:
    """The exact lease identity to pin as replaced, or None for a free/expired row.

    Raises when any recorded holder cannot be PROVEN gone from this host: TTL
    expiry alone is not exit evidence, and a holder on another machine cannot be
    probed here (run recovery there).
    """
    if state.lease is not None:
        if not holder_process_gone(state.lease.holder, held_for_s=state.lease.held_for_s):
            raise ClusterUpdateInProgress(
                f"the cluster deploy lease is held by a live process ({state.lease.holder}) — "
                "recovery refused; wait for it to finish. A holder on another machine cannot "
                "be probed from here — run recovery there."
            )
        if state.lease.acquired_at is None:
            raise ClusterUpdateInProgress(
                "the observed deploy lease lacks its acquired_at identity — recovery refused"
            )
        return (state.lease.holder, state.lease.acquired_at)
    if state.stale_holder is not None and state.stale_holder_probed_gone is not True:
        raise ClusterUpdateInProgress(
            f"the recorded rollout holder {state.stale_holder!r} cannot be proven gone from "
            "this host — recovery refused; run it on that host once its process is dead"
        )
    return None


def pending_publication_recovery_op() -> dict[str, object]:
    """Run the operator recovery for an interrupted rollout's pending publication.

    Returns {"recovered": False, "detail": ...} when there is nothing to recover;
    raises ClusterUpdateInProgress on every refusal. The executable sequence
    (claim, collect, complete) is not connected until the trusted closure
    producer lands: this refuses BEFORE any mutation, never taking an authority
    it cannot finish with. `run_pending_publication_recovery` is that sequence —
    the seat the producer connects to.
    """
    state = read_pending_recovery_state()
    if not state.pending:
        return {"recovered": False, "detail": "no pending publication is journaled"}
    with (
        ui_update_state.resource_lock(purpose="ops.pending_publication_recovery"),
        ui_update_state.lifecycle_lock(),
    ):
        _require_no_live_deploy()
        _prove_abandoned_holder_gone(state)
        raise ClusterUpdateInProgress(_CLOSURE_PRODUCER_NOT_CONNECTED)


def run_pending_publication_recovery(
    *, collect: Callable[[PendingRecoveryClaim], ManagedWriterCollection]
) -> dict[str, object]:
    """The full recovery sequence — the seat the trusted closure producer connects to.

    `collect` must return a fresh complete collection for the REPLACEMENT
    operation (every registered unit observed under the takeover, carrying the
    claim's challenge). It is the trusted rollout producer's job; nothing here
    authenticates Python values — the sequence exists so that producer has
    exactly one reviewed consumer.
    """
    state = read_pending_recovery_state()
    if not state.pending:
        return {"recovered": False, "detail": "no pending publication is journaled"}
    with (
        ui_update_state.resource_lock(purpose="ops.publication_recovery"),
        ui_update_state.lifecycle_lock(),
    ):
        _require_no_live_deploy()
        observed = _prove_abandoned_holder_gone(state)
        claim = claim_abandoned_pending_lease(state, observed=observed)
        collection = collect(claim)
        return complete_pending_publication_recovery(claim, collection)


def claim_abandoned_pending_lease(
    state: PendingRecoveryState, *, observed: tuple[str, datetime] | None
) -> PendingRecoveryClaim:
    """Take over the abandoned rollout lease under a new identity and challenge."""
    if (
        state.abandoned is None
        or state.abandoned_operation_json is None
        or state.candidate_digest is None
    ):
        raise ClusterUpdateInProgress("no pending publication operation to claim; recovery refused")
    claim = claim_pending_recovery_lease(
        self_holder(),
        expected_operation=state.abandoned_operation_json,
        observed=observed,
    )
    if not claim.acquired or claim.acquired_at is None or claim.target_sha is None:
        raise ClusterUpdateInProgress(
            "the deployment journal changed while its dead holder was being proven — "
            "nothing was replaced; re-run recovery"
        )
    operation = RolloutIdentity(
        holder=self_holder(), acquired_at=claim.acquired_at, target_sha=claim.target_sha
    )
    challenge = uuid4()
    insert_event_log(
        event_type="managed_writer_recovery_claimed",
        agent_id=None,
        source="system",
        payload={
            "abandoned_holder": state.abandoned.holder,
            "abandoned_target_sha": state.abandoned.target_sha,
            "previous_holder": claim.previous_holder,
            "new_holder": operation.holder,
            "challenge": str(challenge),
        },
    )
    return PendingRecoveryClaim(
        operation=operation,
        challenge=challenge,
        abandoned=state.abandoned,
        abandoned_units=state.abandoned_units,
        candidate_digest=state.candidate_digest,
        current_publication_id=state.current_publication_id,
    )


def complete_pending_publication_recovery(
    claim: PendingRecoveryClaim, collection: ManagedWriterCollection
) -> dict[str, object]:
    """Replace the abandoned pending operation under the fresh complete closure.

    The replacement CAS lives in `shared.managed_writer_publication`
    (`recover_pending_publication`): it revalidates the journal, rejects the
    abandoned challenge, and requires the fresh collection to cover every
    registered unit for the NEW operation. Agent births stay frozen — recovery
    replaces the pending record, it never publishes `current`.
    """
    with write_transaction(direct=True) as conn:
        state = _locked_publication(conn)
        pending = state.pending
        if pending is None or pending.operation != claim.abandoned:
            raise ClusterUpdateInProgress(
                "the abandoned operation is no longer the journal's pending record — "
                "nothing was replaced"
            )
        replacement = PendingPublication(
            operation=claim.operation,
            predecessor=state.current.publication_id if state.current is not None else None,
            candidate_digest=pending.candidate_digest,
            challenge=claim.challenge,
            units=pending.units,
        )
        try:
            recover_pending_publication(conn, claim.abandoned, replacement, collection)
        except ManagedWriterBarrierError as exc:
            raise ClusterUpdateInProgress(f"recovery evidence was refused: {exc}") from exc
    insert_event_log(
        event_type="managed_writer_recovery_completed",
        agent_id=None,
        source="system",
        payload={
            "abandoned_holder": claim.abandoned.holder,
            "abandoned_target_sha": claim.abandoned.target_sha,
            "new_holder": claim.operation.holder,
            "challenge": str(claim.challenge),
            "units": len(replacement.units),
        },
    )
    return {
        "recovered": True,
        "abandoned_holder": claim.abandoned.holder,
        "new_holder": claim.operation.holder,
        "challenge": str(claim.challenge),
        "units": len(replacement.units),
    }


# ── the exact pre-stop abort (task #4129 C-4) ───────────────────────────────


def _machine_ops_urls() -> dict[str, str | None]:
    """Each machine's pre-resolved ops base URL, one read (the begin's roster shape)."""
    with shared.db.connect(autocommit=True) as conn:
        rows = conn.execute("SELECT name, gateway_url FROM machines ORDER BY name").fetchall()
    return {row[0]: row[1] for row in rows}


async def _read_unit_journals(
    units: tuple[PublishedUnit, ...], urls: dict[str, str | None]
) -> list[BootstrapRecoveryReadResult]:
    # Lazy import: the ops RPC client stays behind the seat's own caller.
    from ops import cluster_rpc

    async def one(unit: PublishedUnit) -> BootstrapRecoveryReadResult:
        result = await cluster_rpc.dispatch_to_machine(
            target_machine=unit.machine,
            kind="cluster_bootstrap_recovery_read",
            payload={},
            ops_url=urls.get(unit.machine),
        )
        return BootstrapRecoveryReadResult.model_validate_json(json.dumps(result))

    return list(await asyncio.gather(*(one(unit) for unit in units)))


def _prove_no_unit_started_a_hop(units: tuple[PublishedUnit, ...]) -> None:
    """Ask every journaled unit for its bootstrap-recovery journal slot; any doubt refuses (C-4).

    Read-only fan-out over the units' ops faces -- the same bearer channel the
    rollout itself uses. An unreachable unit, a failed read, or an answer that
    names another unit refuses: "cannot prove no effect" is never "no effect".
    Runs outside every database transaction (the deployment row lock never
    covers network work); the lease release that follows cuts late starters off.
    """
    from ops import cluster_rpc

    urls = _machine_ops_urls()
    try:
        answers = asyncio.run(_read_unit_journals(units, urls))
    except cluster_rpc.ClusterOpUnreachable as exc:
        raise ClusterUpdateInProgress(
            f"could not prove a journaled unit effect-free ({exc}) -- abort refused"
        ) from exc
    except cluster_rpc.ClusterOpFailed as exc:
        raise ClusterUpdateInProgress(
            f"a bootstrap-recovery read failed ({exc.result!r}) -- abort refused"
        ) from exc
    for unit, answer in zip(units, answers, strict=True):
        if (answer.machine, answer.home) != (unit.machine, unit.home):
            raise ClusterUpdateInProgress(
                "a bootstrap-recovery answer belongs to another unit -- abort refused"
            )
        if answer.journal_present:
            stage = f" (stage {answer.journal_stage})" if answer.journal_stage else ""
            raise ClusterUpdateInProgress(
                f"unit {unit.machine} reports a bootstrap recovery journal{stage}; the exact "
                "pre-stop abort is not usable -- run `ava cluster recover-pending`"
            )


def pre_stop_abort_pending_publication_op() -> dict[str, object]:
    """Clear a never-effective pending journal and release its lease (the exact pre-stop abort).

    Returns {"aborted": False, "detail": ...} when nothing is journaled; raises
    ClusterUpdateInProgress on every refusal. Fail-closed throughout: the
    abandoned holder must be provably gone, every journaled unit must answer
    (and answer "no journal"), and the journal's own record must still be the
    inspected operation with no collection/migration/readbacks -- re-checked
    inside the clearing transaction, whose guarded write is the exactness
    mechanism (the lease release is what refuses any hop child that starts
    after the proof).
    """
    state = read_pending_recovery_state()
    if not state.pending:
        return {"aborted": False, "detail": "no pending publication is journaled"}
    if state.abandoned is None or state.abandoned_operation_json is None:
        raise ClusterUpdateInProgress(
            "the pending publication has no readable operation -- abort refused"
        )
    with (
        ui_update_state.resource_lock(purpose="ops.publication_abort"),
        ui_update_state.lifecycle_lock(),
    ):
        _require_no_live_deploy()
        observed = _prove_abandoned_holder_gone(state)
        _prove_no_unit_started_a_hop(state.abandoned_units)
        with write_transaction(direct=True) as conn:
            publication = _locked_publication(conn)
            pending = publication.pending
            if pending is None or pending.operation != state.abandoned:
                raise ClusterUpdateInProgress(
                    "the pending operation changed while the no-effect proof ran -- "
                    "nothing was cleared; re-run"
                )
            if (
                pending.collection is not None
                or pending.migration is not None
                or pending.unit_readbacks
            ):
                raise ClusterUpdateInProgress(
                    "the pending journal already records effects "
                    "(collection/migration/readbacks); the pre-stop abort is not usable -- "
                    "run `ava cluster recover-pending`"
                )
            cleared = abandon_pending_publication_lease(
                conn,
                expected_operation=state.abandoned_operation_json,
                observed=observed,
            )
            if not cleared:
                raise ClusterUpdateInProgress(
                    "the journal or lease changed while the no-effect proof ran -- "
                    "nothing was cleared; re-run"
                )
    insert_event_log(
        event_type="managed_writer_pre_stop_aborted",
        agent_id=None,
        source="system",
        payload={
            "abandoned_holder": state.abandoned.holder,
            "abandoned_target_sha": state.abandoned.target_sha,
            "units": len(state.abandoned_units),
            "candidate_digest": state.candidate_digest,
        },
    )
    return {
        "aborted": True,
        "abandoned_holder": state.abandoned.holder,
        "target_sha": state.abandoned.target_sha,
        "units": len(state.abandoned_units),
    }
