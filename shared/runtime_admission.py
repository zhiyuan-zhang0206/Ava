"""Loaded-runtime input plus the existing locked publication admission decision."""

import os
from dataclasses import dataclass
from functools import lru_cache

import psycopg

from shared.managed_writer_publication import (
    _ADMISSION_ROW,
    AdmissionDecision,
    CurrentAdmission,
    DeferredAdmission,
    LegacyProtocolZero,
    _admission_state,
    publication_admission,
    publication_admission_async,
)
from shared.runtime_publication_input import (
    RuntimePublicationInput,
    resolve_runtime_publication_input,
    revalidate_runtime_publication_input,
)


class PublicationAdmissionDeferredError(RuntimeError):
    """Maintenance defers birth without terminating a row or consuming inbound."""


@dataclass(frozen=True)
class RuntimeAdmission:
    loaded: RuntimePublicationInput | None

    @classmethod
    def load(cls) -> "RuntimeAdmission":
        """Once per actual process/host boot; never on each inbox claim."""
        return cls(resolve_runtime_publication_input())

    def revalidate(self) -> None:
        if self.loaded is not None:
            revalidate_runtime_publication_input(self.loaded)

    def decide(self, conn: psycopg.Connection) -> AdmissionDecision:
        value = self.loaded
        decision = publication_admission(
            conn,
            value.actual if value else None,
            selector_artifact_digest=value.selector.artifact_digest if value else None,
            selector_manifest_digest=value.selector.manifest_digest if value else None,
        )
        if isinstance(decision, DeferredAdmission):
            raise PublicationAdmissionDeferredError(
                "runtime birth deferred by publication maintenance"
            )
        if isinstance(decision, CurrentAdmission):
            require_activation(conn, decision)
        return decision

    async def decide_async(self, conn: psycopg.AsyncConnection) -> AdmissionDecision:
        value = self.loaded
        decision = await publication_admission_async(
            conn,
            value.actual if value else None,
            selector_artifact_digest=value.selector.artifact_digest if value else None,
            selector_manifest_digest=value.selector.manifest_digest if value else None,
        )
        if isinstance(decision, DeferredAdmission):
            raise PublicationAdmissionDeferredError(
                "runtime birth deferred by publication maintenance"
            )
        if isinstance(decision, CurrentAdmission):
            row = await (await conn.execute(_ADMISSION_ROW)).fetchone()
            _require_activation_state(_admission_state(row), decision)
        return decision


def require_current_for_managed(decision: AdmissionDecision, resource_value: object) -> None:
    """A marker cannot activate managed resources while legacy writers may exist."""
    from shared.incarnation_resources import ResourceBirth, ResourceEvidenceError, decode_resources

    if resource_value is None:
        if isinstance(decision, CurrentAdmission):
            raise ResourceEvidenceError(
                "published runtime cannot infer closure of legacy resources"
            )
        return
    state = decode_resources(resource_value)
    if isinstance(state, ResourceBirth) and state.launch_deadline is None:
        raise ResourceEvidenceError("birth marker has no original bounded launch authority")
    if resource_value is not None and not isinstance(decision, CurrentAdmission):
        raise PublicationAdmissionDeferredError(
            "managed resource admission requires committed publication"
        )


def admitted_caller_protocol(decision: AdmissionDecision) -> int:
    """Only call after the same transaction admitted a complete resource set."""
    from shared.caller_identity import SUPPORTED_CALLER_PROTOCOL

    return SUPPORTED_CALLER_PROTOCOL if isinstance(decision, CurrentAdmission) else 0


def legacy_boot_terminal_allowed(conn: psycopg.Connection) -> bool:
    """Negative-only gate for old unowned boot cleanup, before metadata locks.

    A deferred child has no admitted owner. That absence must not become a
    terminal write after maintenance ends either. Only the exact never-enabled
    legacy state permits that old fallback; managed attempts use their own
    bounded launch authority. Corrupt evidence raises instead of falling back.
    """
    from psycopg.pq import TransactionStatus

    if conn.info.transaction_status != TransactionStatus.INTRANS:
        raise RuntimeError("boot terminal guard requires the caller transaction")
    return isinstance(_admission_state(conn.execute(_ADMISSION_ROW).fetchone()), LegacyProtocolZero)


@lru_cache(maxsize=1)
def _process_boot(pid: int) -> RuntimeAdmission:
    if pid != os.getpid():
        raise RuntimeError("runtime input must belong to this actual process")
    return RuntimeAdmission.load()


def process_runtime_admission() -> RuntimeAdmission:
    """A fork has a different key; no inherited verified object grants admission."""
    value = _process_boot(os.getpid())
    value.revalidate()
    return value


def require_activation(conn: psycopg.Connection, decision: AdmissionDecision) -> None:
    """Foundation v2 current records are not positive all-writer publication."""
    _require_activation_state(_admission_state(conn.execute(_ADMISSION_ROW).fetchone()), decision)


def _require_activation_state(state: object, decision: AdmissionDecision) -> None:
    from shared.managed_writer_publication import WriterPublication

    if not isinstance(decision, CurrentAdmission):
        raise PublicationAdmissionDeferredError("managed birth requires current publication")
    if (
        not isinstance(state, WriterPublication)
        or state.current is None
        or state.current.publication_id != decision.publication_id
        or state.current.activation_digest is None
        or state.current.activation_challenge is None
    ):
        raise PublicationAdmissionDeferredError("current publication lacks verified activation")
