"""Loaded-runtime input plus the existing locked publication admission decision."""

from dataclasses import dataclass

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
        return decision


def require_current_for_managed(decision: AdmissionDecision, resource_value: object) -> None:
    """A marker cannot activate managed resources while legacy writers may exist."""
    if resource_value is not None and not isinstance(decision, CurrentAdmission):
        raise PublicationAdmissionDeferredError(
            "managed resource admission requires committed publication"
        )


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
