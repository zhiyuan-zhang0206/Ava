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

A row is converted only when a successor would then take it
(`successor_refusal`, which the read-only survey applies too): converting a
shape resurrection or admission still refuses would leave the agent fenced
with no conversion left to retry.

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
from typing import Literal, LiteralString
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


# The agents_meta facts `successor_refusal` reads, in `SuccessorRow` field order.
SUCCESSOR_COLUMNS: LiteralString = (
    "status,runtime_kind,runtime_generation,runtime_owner,pid,lifecycle_command_id,"
    "(lease_expires_at IS NULL OR lease_expires_at<=clock_timestamp()) AS lease_free"
)


@dataclass(frozen=True)
class SuccessorRow:
    """The `agents_meta` fields that decide whether a successor could take the row."""

    status: str
    runtime_kind: str | None
    runtime_generation: UUID | None
    runtime_owner: UUID | None
    pid: int | None
    lifecycle_command_id: int | None
    lease_free: bool


@dataclass(frozen=True)
class SettledReceipt:
    """A receipt satisfying `PREDECESSOR_RECEIPT` for the recorded incarnation."""

    id: int
    kind: Literal["restart", "terminate"]
    closed_already: bool  # it already carries a cutover closure


@dataclass(frozen=True)
class Refusal:
    """`inadmissible` may still change (a live runtime, an unsettled command, no
    receipt yet); `unconvertible` is a shape no successor path accepts."""

    verdict: Literal["inadmissible", "unconvertible"]
    reason: str


def successor_refusal(
    row: SuccessorRow, closed: tuple[UUID, UUID], receipt: SettledReceipt | None
) -> Refusal | None:
    """Why the converted row would not be taken by its successor; None when it would.

    The closed-predecessor form meets exactly two successor paths, and this is
    their admission read off the row:

    - An idling row whose owner was released (runtime generation and owner
      NULL, kind NULL or hosted) is claimed by hosted admission. Its receipt is
      the drain's restart, held as the lifecycle pointer and observed by the
      admission, or an applied and observed terminate with no pointer left:
      admission clears only a restart pointer.
    - A terminated row resurrects only while it still records exactly the
      closed incarnation as its hosted runtime and holds no lifecycle pointer
      (`ops.resurrection_retry.hosted_resurrection_target` and the final CAS in
      `ops.agent_wake`); the claim that follows consumes the terminate receipt.

    Anything live or different owning the row refuses before the shape is
    judged, and a receipt that already carries a closure never converts twice.
    """
    return _owned(row, closed) or _shape(row, closed) or _receipted(row, receipt)


def _owned(row: SuccessorRow, closed: tuple[UUID, UUID]) -> Refusal | None:
    runtime = (row.runtime_generation, row.runtime_owner)
    live = row.pid is not None or not row.lease_free or row.status not in ("idling", "terminated")
    if live or runtime not in ((None, None), closed) or (row.status, runtime) == ("idling", closed):
        return Refusal("inadmissible", "the row still names a live or different incarnation")
    return None


def _shape(row: SuccessorRow, closed: tuple[UUID, UUID]) -> Refusal | None:
    if row.runtime_kind not in (None, "hosted"):
        reason = "the row records a process runtime; no hosted successor admits it"
    elif row.status != "terminated":
        return None
    elif (row.runtime_generation, row.runtime_owner) != closed:
        reason = (
            "the terminated row released its runtime identity; resurrection needs the closed "
            "hosted incarnation"
        )
    elif row.runtime_kind != "hosted":
        reason = "the terminated row records no hosted runtime kind; resurrection refuses it"
    else:
        return None
    return Refusal("unconvertible", reason)


def _receipted(row: SuccessorRow, receipt: SettledReceipt | None) -> Refusal | None:
    pointer = row.lifecycle_command_id
    if pointer is not None and (receipt is None or pointer != receipt.id):
        return Refusal("inadmissible", "the lifecycle pointer names an unsettled command")
    if receipt is None:
        return Refusal("inadmissible", "no settled lifecycle receipt for the recorded incarnation")
    if receipt.closed_already:
        return Refusal("inadmissible", "the receipt already carries a cutover closure")
    if pointer is None:
        return None
    if row.status == "terminated":
        return Refusal(
            "unconvertible",
            "the terminated row still points at its receipt; resurrection defers while any "
            "lifecycle pointer is set",
        )
    if receipt.kind != "restart":
        return Refusal(
            "unconvertible",
            "the lifecycle pointer still names the terminate receipt; admission clears only a "
            "restart pointer",
        )
    return None


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
    compare-and-swap, so a replay or a concurrent writer refuses), the attested
    machine, and `successor_refusal` over the row and the receipt, which must
    satisfy the predecessor rule for the incarnation the before image names.
    """
    if conn.info.transaction_status != TransactionStatus.INTRANS:
        raise ResourceEvidenceError("predecessor closure requires an explicit transaction")
    retired = _retired_identity(before)
    closed = (retired.generation, retired.owner)
    row = conn.execute(
        f"SELECT {SUCCESSOR_COLUMNS},machine,incarnation_resources=%s "  # noqa: S608 -- constant columns
        "FROM agents_meta WHERE id=%s FOR UPDATE",
        (Jsonb(before), agent_id),
    ).fetchone()
    if row is None:
        raise ResourceEvidenceError("predecessor closure target does not exist")
    if row[8] is not True:
        raise ResourceEvidenceError("stored resources differ from the recorded before image")
    if row[7] != evidence.machine:
        raise ResourceEvidenceError("closure attestation belongs to another machine")
    found = conn.execute(
        "SELECT i.kind, COALESCE(i.payload ? 'cutover_closure', false) FROM inbound_messages i "  # noqa: S608 -- constant SQL fragment
        f"JOIN agents_meta m ON m.id=i.agent_id WHERE i.id=%s AND {PREDECESSOR_RECEIPT} "
        "FOR UPDATE OF i",
        (receipt, agent_id, *closed),
    ).fetchone()
    settled = None if found is None else SettledReceipt(receipt, found[0], found[1])
    refusal = successor_refusal(SuccessorRow(*row[:7]), closed, settled)
    if refusal is not None:
        raise ResourceEvidenceError(refusal.reason)
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
