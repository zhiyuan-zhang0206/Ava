"""Complete per-incarnation exec allocation set, serialized by agents_meta.

These are server-owned facts, never request payloads or configuration. The
caller retains one explicit transaction through registration/force acceptance;
no process launch, filesystem read or network wait belongs under that lock.
"""

import json
from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

import psycopg
from psycopg.pq import TransactionStatus
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

from shared.runtime_incarnation import RuntimeIncarnation


class ResourceEvidenceError(RuntimeError):
    """Missing, stale or incomplete evidence cannot authorize resource work."""


class _StrictEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class ResourceBirth(_StrictEvidence):
    """Stamped only by a fresh metadata INSERT, not an empty-set assertion."""

    version: Literal[1] = 1
    state: Literal["unadmitted"] = "unadmitted"
    birth: UUID


class ResourceProcess(_StrictEvidence):
    pid: int = Field(gt=0)
    birth: float = Field(gt=0, allow_inf_nan=False)


class ExecAllocation(_StrictEvidence):
    request: UUID
    domain: UUID
    request_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    deadline: datetime
    owner_process: ResourceProcess | None = None
    root_process: ResourceProcess | None = None

    @model_validator(mode="after")
    def require_complete_identity(self) -> "ExecAllocation":
        if (self.owner_process is None) != (self.root_process is None):
            raise ValueError("exec owner/root identities must be attached together")
        if self.deadline.tzinfo is None:
            raise ValueError("exec deadline must be timezone-aware")
        return self


class IncarnationResources(_StrictEvidence):
    version: Literal[1] = 1
    state: Literal["admitted"] = "admitted"
    generation: UUID
    owner: UUID
    frozen_by: int | None = Field(default=None, gt=0)
    requests: dict[str, ExecAllocation]

    @model_validator(mode="after")
    def require_exact_keys(self) -> "IncarnationResources":
        if any(key != str(entry.request) for key, entry in self.requests.items()):
            raise ValueError("resource entry key differs from its exact request")
        return self


ResourceState = Annotated[ResourceBirth | IncarnationResources, Field(discriminator="state")]
_STATE = TypeAdapter(ResourceState)


def decode_resources(value: object) -> ResourceBirth | IncarnationResources:
    if value is None:
        raise ResourceEvidenceError("incarnation resource set is unknown")
    return _STATE.validate_json(json.dumps(value))


def _transaction(conn: psycopg.Connection) -> None:
    if conn.info.transaction_status != TransactionStatus.INTRANS:
        raise ResourceEvidenceError("resource evidence requires an explicit transaction")


def _locked(
    conn: psycopg.Connection, target: RuntimeIncarnation
) -> tuple[IncarnationResources, str, int | None]:
    _transaction(conn)
    row = conn.execute(
        "SELECT runtime_generation,runtime_owner,incarnation_resources,status,lifecycle_command_id "
        "FROM agents_meta WHERE id=%s FOR UPDATE",
        (target.agent_id,),
    ).fetchone()
    if row is None or row[:2] != (target.generation, target.owner):
        raise ResourceEvidenceError("resource writer lost its runtime incarnation")
    evidence = decode_resources(row[2])
    if not isinstance(evidence, IncarnationResources) or (evidence.generation, evidence.owner) != (
        target.generation,
        target.owner,
    ):
        raise ResourceEvidenceError("resource set belongs to another incarnation")
    return evidence, row[3], row[4]


def _store(
    conn: psycopg.Connection, target: RuntimeIncarnation, evidence: IncarnationResources
) -> None:
    conn.execute(
        "UPDATE agents_meta SET incarnation_resources=%s WHERE id=%s",
        (Jsonb(evidence.model_dump(mode="json")), target.agent_id),
    )


def admit_first_resources(
    conn: psycopg.Connection, target: RuntimeIncarnation, birth: UUID
) -> None:
    """Consume a fresh server INSERT marker in the actual first admission TX.

    This does not admit the runtime itself. The caller must complete normal
    placement/session/admission checks and owner stamping in this transaction.
    Historical NULL and previously admitted rows are deliberately ineligible.
    """
    _transaction(conn)
    row = conn.execute(
        "SELECT incarnation_resources,runtime_generation,runtime_owner,runtime_kind,pid,"
        "started_at,lifecycle_command_id,status FROM agents_meta WHERE id=%s FOR UPDATE",
        (target.agent_id,),
    ).fetchone()
    if row is None or row[1:7] != (None, None, None, None, None, None) or row[7] != "idling":
        raise ResourceEvidenceError("first resource admission requires a never-admitted birth")
    marker = decode_resources(row[0])
    if not isinstance(marker, ResourceBirth) or marker.birth != birth:
        raise ResourceEvidenceError("first resource admission lost its exact birth marker")
    _store(
        conn,
        target,
        IncarnationResources(generation=target.generation, owner=target.owner, requests={}),
    )


def register_exec(
    conn: psycopg.Connection, target: RuntimeIncarnation, entry: ExecAllocation
) -> None:
    """Reserve before root permit; an ambiguous commit is not permission to run."""
    evidence, status, pointer = _locked(conn, target)
    if status not in {"running", "idling"} or pointer is not None or evidence.frozen_by is not None:
        raise ResourceEvidenceError("incarnation is frozen for lifecycle work")
    if entry.owner_process is not None or str(entry.request) in evidence.requests:
        raise ResourceEvidenceError("exec allocation is already attached or registered")
    fresh = conn.execute(
        "SELECT lease_expires_at>clock_timestamp() AND %s>clock_timestamp() "
        "FROM agents_meta WHERE id=%s",
        (entry.deadline, target.agent_id),
    ).fetchone()
    if fresh != (True,):
        raise ResourceEvidenceError("exec registration requires fresh runtime lease and deadline")
    _store(
        conn,
        target,
        evidence.model_copy(update={"requests": evidence.requests | {str(entry.request): entry}}),
    )


def freeze_resources(conn: psycopg.Connection, target: RuntimeIncarnation, command_id: int) -> None:
    """Freeze the complete stored set only for the actual accepted force pointer."""
    evidence, status, pointer = _locked(conn, target)
    if status != "terminated" or pointer != command_id:
        raise ResourceEvidenceError("resource freeze does not own the force pointer")
    command = conn.execute(
        "SELECT id FROM inbound_messages WHERE id=%s AND agent_id=%s AND kind='terminate' "
        "AND status='claimed' AND applied_at IS NOT NULL AND observed_at IS NULL "
        "AND target_generation=%s AND target_owner=%s FOR UPDATE",
        (command_id, target.agent_id, target.generation, target.owner),
    ).fetchone()
    if command != (command_id,) or (
        evidence.frozen_by is not None and evidence.frozen_by > command_id
    ):
        raise ResourceEvidenceError("resource freeze has stale command authority")
    _store(conn, target, evidence.model_copy(update={"frozen_by": command_id}))


def attach_exec(
    conn: psycopg.Connection,
    target: RuntimeIncarnation,
    reserved: ExecAllocation,
    attached: ExecAllocation,
) -> None:
    """Bind actual owner/root before releasing user code; force wins this race."""
    evidence, status, pointer = _locked(conn, target)
    if evidence.frozen_by is not None or pointer is not None or status not in {"running", "idling"}:
        raise ResourceEvidenceError("force prevents exec user-code admission")
    if (
        evidence.requests.get(str(reserved.request)) != reserved
        or reserved.owner_process is not None
    ):
        raise ResourceEvidenceError("exec reservation changed before attachment")
    if (
        attached.owner_process is None
        or attached.model_copy(update={"owner_process": None, "root_process": None}) != reserved
    ):
        raise ResourceEvidenceError("attached domain does not match the original allocation")
    if conn.execute(
        "SELECT %s>clock_timestamp() AND lease_expires_at>clock_timestamp() "
        "FROM agents_meta WHERE id=%s",
        (reserved.deadline, target.agent_id),
    ).fetchone() != (True,):
        raise ResourceEvidenceError("exec allocation expired before user code")
    _store(
        conn,
        target,
        evidence.model_copy(
            update={"requests": evidence.requests | {str(reserved.request): attached}}
        ),
    )
