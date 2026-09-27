"""One-time cutover conversion of a retired-writer resource row into the
closed-predecessor form (FC-4a).

The retired runtime recorded `ResourceProcess` receipts without native boot
scope, so its `agents_meta.incarnation_resources` values no longer decode. The
runtime keeps no parser for that shape. The cutover reconciler instead replaces
each such value, one row at a time, with

    IncarnationResources(generation=G, owner=O, host_process=None, requests={})

where (G, O) is the incarnation a settled lifecycle receipt closed: the old
drain's applied restart, still held as the row's lifecycle pointer, or an
applied and observed terminate. That receipt is exactly
`resource_admission.PREDECESSOR_RECEIPT`, so the ordinary successor admission
accepts the form once — it rewrites the set for its own incarnation and, for a
restart, observes the receipt — and no other runtime path changes. A same-owner
continuation refuses it (no host identity), and nothing else can accept it.

Allocation closure of the retired writer is not provable from the database.
The caller supplies the digest of that machine's closure attestation (each
recorded process absent or its boot ended, and the home census empty); it is
stored verbatim, with the before image and the operator, on the receipt in the
same transaction. This module never interprets retired process identities: it
reads only the fields both models share (version, state, generation, owner).

NULL is unknown and stays protocol zero; a current-model value already carries
its own proof; neither is ever converted here. Only the one-time cutover
script calls this module, and it is deleted with that script.
"""

from dataclasses import dataclass
from typing import Literal
from uuid import UUID

import psycopg
from psycopg.pq import TransactionStatus
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from shared.incarnation_resources import (
    IncarnationResources,
    ResourceEvidenceError,
    ResourceShapeError,
    decode_resources,
)
from shared.resource_admission import PREDECESSOR_RECEIPT


class ClosureEvidence(BaseModel):
    """Operator-recorded proof that every native writer of the row ended."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    machine: str = Field(min_length=1)
    attestation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    operator: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class _RetiredIdentity(BaseModel):
    """The incarnation identity fields the retired and current models share."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    version: Literal[1]
    state: Literal["admitted"]
    generation: UUID
    owner: UUID


@dataclass(frozen=True)
class PredecessorClosure:
    agent_id: int
    receipt: int
    before: object
    after: IncarnationResources


def _decodes(value: object) -> bool:
    try:
        decode_resources(value)
    except ResourceShapeError:
        return False
    return True


def _retired_identity(before: object) -> _RetiredIdentity:
    if before is None:
        raise ResourceEvidenceError(
            "unknown (NULL) resources stay protocol zero; the cutover never converts them"
        )
    if _decodes(before):
        raise ResourceEvidenceError(
            "row already carries current resource evidence; its successor proves its own "
            "predecessor"
        )
    try:
        return _RetiredIdentity.model_validate(before)
    except ValidationError as exc:
        raise ResourceEvidenceError("retired resource value names no admitted incarnation") from exc


def _lock_released_row(
    conn: psycopg.Connection,
    agent_id: int,
    before: object,
    closed: tuple[UUID, UUID],
    receipt: int,
) -> str:
    """Lock the row and require it to hold no live, different or unsettled runtime.

    Released (restart apply or resurrection cleared the owner) or terminated
    with exactly the closed incarnation still recorded; no process runtime, no
    live lease, and no lifecycle pointer other than the receipt itself.
    Returns the row's machine.
    """
    row = conn.execute(
        "SELECT status,machine,runtime_kind,runtime_generation,runtime_owner,pid,"
        "lifecycle_command_id,lease_expires_at IS NULL OR lease_expires_at<=clock_timestamp(),"
        "incarnation_resources=%s FROM agents_meta WHERE id=%s FOR UPDATE",
        (Jsonb(before), agent_id),
    ).fetchone()
    if row is None:
        raise ResourceEvidenceError("predecessor closure target does not exist")
    status, machine, kind, generation, owner, pid, pointer, lease_free, unchanged = row
    if unchanged is not True:
        raise ResourceEvidenceError("stored resources differ from the recorded before image")
    released = (generation, owner) == (None, None) and status in ("idling", "terminated")
    terminated = (generation, owner) == closed and status == "terminated"
    if not (released or terminated) or pid is not None or kind not in (None, "hosted"):
        raise ResourceEvidenceError("a live or different incarnation owns this row")
    if lease_free is not True or pointer not in (None, receipt):
        raise ResourceEvidenceError("the row still holds a live lease or an unsettled command")
    return machine


def close_retired_predecessor(
    conn: psycopg.Connection,
    agent_id: int,
    *,
    before: object,
    receipt: int,
    evidence: ClosureEvidence,
) -> PredecessorClosure:
    """Replace one retired-shape value with the closed-predecessor form.

    The caller owns the explicit transaction and commits it. Everything is
    checked under the agents_meta row lock: the exact before image (a
    compare-and-swap, so a replay or a concurrent writer refuses), the receipt
    against the predecessor rule for the incarnation the before image names,
    no live or different incarnation on the row, and the attested machine.
    """
    if conn.info.transaction_status != TransactionStatus.INTRANS:
        raise ResourceEvidenceError("predecessor closure requires an explicit transaction")
    retired = _retired_identity(before)
    closed = (retired.generation, retired.owner)
    if _lock_released_row(conn, agent_id, before, closed, receipt) != evidence.machine:
        raise ResourceEvidenceError("closure attestation belongs to another machine")
    found = conn.execute(
        "SELECT COALESCE(i.payload ? 'cutover_closure', false) FROM inbound_messages i "  # noqa: S608 -- constant SQL fragment
        f"JOIN agents_meta m ON m.id=i.agent_id WHERE i.id=%s AND {PREDECESSOR_RECEIPT} "
        "FOR UPDATE OF i",
        (receipt, agent_id, *closed),
    ).fetchone()
    if found is None:
        raise ResourceEvidenceError(
            "receipt is not a settled lifecycle decision for the recorded incarnation"
        )
    if found[0] is True:
        raise ResourceEvidenceError("receipt already carries a cutover closure")
    after = IncarnationResources(generation=retired.generation, owner=retired.owner, requests={})
    conn.execute(
        "UPDATE agents_meta SET incarnation_resources=%s WHERE id=%s",
        (Jsonb(after.model_dump(mode="json")), agent_id),
    )
    conn.execute(
        "UPDATE inbound_messages SET payload=COALESCE(payload,'{}'::jsonb)||"
        "jsonb_build_object('cutover_closure',%s::jsonb||"
        "jsonb_build_object('recorded_at',clock_timestamp())) WHERE id=%s",
        (Jsonb({"before": before, **evidence.model_dump(mode="json")}), receipt),
    )
    return PredecessorClosure(agent_id=agent_id, receipt=receipt, before=before, after=after)
