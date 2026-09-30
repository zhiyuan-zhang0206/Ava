"""The locked publication admission decision for a hosted runtime birth."""

import psycopg

from base.deploy.writers.publication import (
    _ADMISSION_LOCK,
    _ADMISSION_ROW,
    AdmissionDecision,
    CurrentAdmission,
    DeferredAdmission,
    _admission_state,
    publication_admission,
    publication_admission_async,
)


class PublicationAdmissionDeferredError(RuntimeError):
    """Maintenance defers birth without terminating a row or consuming inbound."""


class RuntimeAdmission:
    """Admit one hosted runtime birth under the caller's transaction."""

    def decide(self, conn: psycopg.Connection) -> AdmissionDecision:
        decision = publication_admission(conn)
        if isinstance(decision, DeferredAdmission):
            raise PublicationAdmissionDeferredError(
                "runtime birth deferred by publication maintenance"
            )
        if isinstance(decision, CurrentAdmission):
            require_activation(conn, decision)
        return decision

    async def decide_async(self, conn: psycopg.AsyncConnection) -> AdmissionDecision:
        decision = await publication_admission_async(conn)
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
    decide/decide_async, a committed publication refuses a row whose
    resource closure cannot be inferred, and a stored value the current model
    cannot decode (a retired writer's shape awaiting cutover reconciliation)
    refuses under every decision. Raises ResourceEvidenceError so the hosted
    caller converts the fence into a recorded refusal.
    """
    from base.agents.incarnation.resources import ResourceEvidenceError, decode_resources

    if resource_value is None:
        if isinstance(decision, CurrentAdmission):
            raise ResourceEvidenceError(
                "published runtime cannot infer closure of legacy resources"
            )
        return
    decode_resources(resource_value)


def require_activation(conn: psycopg.Connection, decision: AdmissionDecision) -> None:
    """Foundation v2 current records are not positive all-writer publication."""
    conn.execute(_ADMISSION_LOCK)
    _require_activation_state(_admission_state(conn.execute(_ADMISSION_ROW).fetchone()), decision)


def _require_activation_state(state: object, decision: AdmissionDecision) -> None:
    from base.deploy.writers.publication import WriterPublication

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
