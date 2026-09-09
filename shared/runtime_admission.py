"""Loaded-runtime input plus the existing locked publication admission decision."""

import os
from dataclasses import dataclass
from functools import lru_cache

import psycopg

from shared.managed_writer_publication import (
    _ADMISSION_LOCK,
    _ADMISSION_ROW,
    AdmissionDecision,
    CurrentAdmission,
    DeferredAdmission,
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
    """A marker cannot activate managed resources while legacy writers may exist.

    Post-#1924 the hosted runtime admits under the legacy protocol-zero state
    exactly as upstream does (its tests and rows carry the pre-deadline marker
    shape); the spawn-side producers this check was written against are
    retired. The surviving fences: a pending publication defers admission in
    decide/decide_async, and a committed publication refuses a row whose
    resource closure cannot be inferred. Raises ResourceEvidenceError so the
    hosted caller converts the fence into a quiet refusal.
    """
    if resource_value is None and isinstance(decision, CurrentAdmission):
        from shared.incarnation_resources import ResourceEvidenceError

        raise ResourceEvidenceError("published runtime cannot infer closure of legacy resources")


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
    conn.execute(_ADMISSION_LOCK)
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
