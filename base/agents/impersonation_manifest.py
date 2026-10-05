"""Local producer receipts and central staging for impersonation event logs.

A controller's SDK events are appended to the lease's log at the capture seam,
before the telemetry queue, while its receipt is open; transactional central
audit events are appended in their producing transaction. The receipts and the
completeness predicate live in the database (`event_log`).
"""

from __future__ import annotations

from collections.abc import Callable, Generator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar  # noqa: TID251 -- SDK async finally needs task-local admission
from dataclasses import dataclass, field, replace
from threading import Condition, Lock
from typing import Any

import psycopg
from psycopg.rows import dict_row

from base.agents.impersonation import lock_lease
from base.agents.impersonation.event_log import (
    CENTRAL_SOURCE,
    LOG_PROTOCOL_VERSION,
    append_source_event,
    is_log_native,
    locked_receipt_state,
    refresh_completed_export,
)
from base.db import Database
from base.log import logger
from base.telemetry import Event


@dataclass(frozen=True)
class LocalParticipant:
    """The one controller process that may capture this process's events."""

    lease_id: str
    agent_id: int
    session_id: int
    source_key: str
    db: Database = field(compare=False, repr=False)


@dataclass
class _CaptureGate:
    """Per-participant close fence for local producer work."""

    participant: LocalParticipant
    admission_closed: bool = False
    in_flight: int = 0
    capture_failure_pending: bool = False
    condition: Condition = field(default_factory=Condition, repr=False)

    def admit(self, *, fail_closed_capture: bool = False) -> _CaptureAdmission | None:
        with self.condition:
            if self.admission_closed:
                if fail_closed_capture:
                    self.capture_failure_pending = True
                return None
            self.in_flight += 1
        return _CaptureAdmission(self)

    def close_admission(self) -> None:
        with self.condition:
            self.admission_closed = True

    def begin_close(self, mark_closing: Callable[[], None]) -> None:
        """Atomically mark attachment close and reject later SDK admissions."""
        with self.condition:
            mark_closing()
            self.admission_closed = True

    def close_and_wait(self, timeout: float) -> bool:
        self.close_admission()
        with self.condition:
            self.condition.wait_for(lambda: self.in_flight == 0, timeout=timeout)
            return self.in_flight == 0


@dataclass
class _CaptureAdmission:
    """One admitted SDK call that may still emit after close starts."""

    gate: _CaptureGate
    released: bool = False

    def release(self) -> None:
        if self.released:
            return
        self.released = True
        _release_capture_admission(self.gate)


_participant_lock = Lock()
_active_participant: LocalParticipant | None = None
_active_capture_gate: _CaptureGate | None = None
_capture_gates: dict[LocalParticipant, _CaptureGate] = {}
_sdk_capture_admission: ContextVar[_CaptureAdmission | None] = ContextVar(
    "impersonation_event_sdk_capture_admission", default=None
)


def session_tag(agent_id: int, session_id: int) -> str:
    """Return the immutable correlation value shared by receipts and Loki."""
    return f"{agent_id}:{session_id}"


def pending_reason(lease: Mapping[str, Any]) -> str | None:
    """Classify a handoff's current delivery uncertainty without inventing state."""
    if lease["events_completed_at"] is not None:
        return None
    if lease["automatic"] is not True:
        return "manual"
    if lease["event_delivery_protocol_version"] != LOG_PROTOCOL_VERSION:
        return "legacy"
    stored = lease["event_delivery_pending_reason"]
    if stored is not None:
        return str(stored)
    return "awaiting_session_end" if lease["ended_at"] is None else "awaiting_participant_seal"


def open_local_participant(db: Database, lease_id: str, *, agent_id: int, source_key: str) -> bool:
    """Open one controller receipt before it can emit an eligible event."""
    with db.write_transaction() as conn:
        lease = lock_lease(conn, lease_id)
        if not is_log_native(lease):
            return False
        if lease["agent_id"] != agent_id:
            raise RuntimeError("Local receipt belongs to another agent")
        if lease["event_admission_closed_at"] is not None:
            raise RuntimeError("Impersonation event admission is closed")
        conn.execute(
            "INSERT INTO agent_impersonation_event_participants(lease_id,source_key,state) "
            "VALUES(%s,%s,'open') ON CONFLICT (lease_id,source_key) DO NOTHING",
            (lease_id, source_key),
        )
    return True


def bind_local_participant(participant: LocalParticipant) -> None:
    """Bind the current external controller to its already durable receipt."""
    global _active_capture_gate, _active_participant  # noqa: PLW0603 - one external attachment per process
    with _participant_lock:
        if _active_participant is not None:
            raise RuntimeError("An impersonation event participant is already bound")
        _active_participant = participant
        gate = _CaptureGate(participant)
        _capture_gates[participant] = gate
        _active_capture_gate = gate


def unbind_local_participant(participant: LocalParticipant) -> None:
    """Remove a participant binding only after its receipt closure path ran."""
    global _active_capture_gate, _active_participant  # noqa: PLW0603 - one external attachment per process
    with _participant_lock:
        if _active_participant == participant:
            _active_participant = None
            _active_capture_gate = None


def _bound_participant() -> LocalParticipant | None:
    with _participant_lock:
        return _active_participant


def _bound_capture_gate() -> _CaptureGate | None:
    with _participant_lock:
        return _active_capture_gate


def _capture_gate(participant: LocalParticipant) -> _CaptureGate | None:
    with _participant_lock:
        return _capture_gates.get(participant)


def admit_local_sdk_call() -> _CaptureAdmission | None:
    """Admit one SDK call before its body can defer telemetry to ``finally``."""
    gate = _bound_capture_gate()
    return None if gate is None else gate.admit()


@contextmanager
def admitted_local_sdk_call() -> Generator[None, None, None]:
    """Carry one pre-close SDK admission through its eventual emit ``finally``."""
    admission = admit_local_sdk_call()
    token = _sdk_capture_admission.set(admission)
    try:
        yield
    finally:
        _sdk_capture_admission.reset(token)
        if admission is not None:
            admission.release()


def local_sdk_call_was_admitted() -> bool:
    """Whether this SDK call crossed the capture gate before close started."""
    return _sdk_capture_admission.get() is not None


def begin_local_participant_close(
    participant: LocalParticipant, mark_closing: Callable[[], None]
) -> None:
    """Atomically begin attachment close and fence new local SDK admissions."""
    gate = _capture_gate(participant)
    if gate is None:
        raise RuntimeError("Missing local impersonation event capture gate")
    gate.begin_close(mark_closing)


def close_local_participant_admission(participant: LocalParticipant, *, timeout: float) -> bool:
    """Close new local admissions and bounded-wait for already admitted work."""
    gate = _capture_gate(participant)
    if gate is None:
        raise RuntimeError("Missing local impersonation event capture gate")
    return gate.close_and_wait(timeout)


def _release_capture_admission(gate: _CaptureGate) -> None:
    should_seal = False
    with gate.condition:
        if gate.in_flight <= 0:
            raise RuntimeError("Impersonation event capture admission underflow")
        gate.in_flight -= 1
        gate.condition.notify_all()
        should_seal = gate.admission_closed and gate.in_flight == 0
    if should_seal:
        try:
            seal_local_participant(gate.participant)
        except Exception:
            logger.exception("Could not seal drained impersonation event receipt")


def _is_local_eligible(event: Event, participant: LocalParticipant) -> bool:
    if event.event_name == "sdk_call":
        return event.agent_id == participant.agent_id
    return event.category == "audit" and (
        event.source == f"agent:{participant.agent_id}"
        or (event.source == "self" and event.agent_id == participant.agent_id)
    )


def capture_local_event(event: Event) -> Event:
    """Tag and record an eligible local event before the telemetry queue.

    A writer failure marks the receipt failed whenever the database is
    reachable, leaving the lease honestly pending instead of making a
    best-effort sink failure look complete. A direct event after closure is
    refused because it cannot be added to the sealed receipt.
    """
    admission = _sdk_capture_admission.get()
    participant = admission.gate.participant if admission is not None else _bound_participant()
    if participant is None or not _is_local_eligible(event, participant):
        return event
    transient_admission: _CaptureAdmission | None = None
    if admission is None:
        gate = _bound_capture_gate()
        if gate is None or gate.participant != participant:
            return event
        transient_admission = gate.admit(fail_closed_capture=True)
        if transient_admission is None:
            _mark_participant_failed(participant)
            raise RuntimeError("Impersonation event capture is closed")
    tagged = replace(
        event,
        attributes={
            **event.attributes,
            "impersonation_session": session_tag(participant.agent_id, participant.session_id),
        },
    )
    try:
        _insert_local_item(participant, tagged)
    except Exception:
        logger.exception("Impersonation event local capture failed")
        _mark_participant_failed(participant)
    finally:
        if transient_admission is not None:
            transient_admission.release()
    return tagged


def _insert_local_item(participant: LocalParticipant, event: Event) -> None:
    with participant.db.write_transaction() as conn:
        lease = lock_lease(conn, participant.lease_id)
        if not is_log_native(lease):
            raise RuntimeError("Local receipt belongs to a lease without an event log")
        state = locked_receipt_state(conn, participant.lease_id, participant.source_key)
        if state != "open":
            raise RuntimeError("Local receipt is not open for event capture")
        append_source_event(conn, lease, event, source_key=participant.source_key)


def _mark_participant_failed(participant: LocalParticipant) -> None:
    """Record a capture failure without allowing a transient writer loss to erase it."""
    gate = _capture_gate(participant)
    if gate is not None:
        with gate.condition:
            gate.capture_failure_pending = True
    try:
        _persist_capture_failure(participant)
    except Exception:
        logger.exception("Could not record impersonation event capture failure")
        return
    if gate is not None:
        with gate.condition:
            gate.capture_failure_pending = False


def _persist_capture_failure(participant: LocalParticipant) -> None:
    """Durably turn an open receipt into failed before alert delivery is attempted."""
    with participant.db.write_transaction() as conn:
        lease = lock_lease(conn, participant.lease_id)
        state = locked_receipt_state(conn, participant.lease_id, participant.source_key)
        if state is None:
            raise RuntimeError("Missing local impersonation event receipt")
        if state == "open":
            conn.execute(
                "SELECT seal_impersonation_event_participant(%s,%s,'failed','capture_failed',NULL)",
                (participant.lease_id, participant.source_key),
            )
            conn.execute(
                "UPDATE agent_impersonations SET event_delivery_pending_reason='capture_failed' "
                "WHERE id=%s AND events_completed_at IS NULL",
                (participant.lease_id,),
            )
        elif state != "failed":
            raise RuntimeError("Cannot record a capture failure after receipt sealing")
        # Keep ``lease`` live so its dict-row shape is checked.
        if str(lease["id"]) != participant.lease_id:
            raise RuntimeError("Local receipt belongs to another lease")


def seal_local_participant(participant: LocalParticipant) -> None:
    """Seal one receipt after its controller has finished admitted work."""
    gate = _capture_gate(participant)
    if gate is not None:
        with gate.condition:
            retry_capture_failure = gate.capture_failure_pending
        if retry_capture_failure:
            _mark_participant_failed(participant)
            with gate.condition:
                if gate.capture_failure_pending:
                    raise RuntimeError("Local capture failure is not durably recorded")
    with participant.db.write_transaction() as conn:
        lease = lock_lease(conn, participant.lease_id)
        if not is_log_native(lease):
            return
        state = locked_receipt_state(conn, participant.lease_id, participant.source_key)
        if state is None:
            raise RuntimeError("Missing local impersonation event receipt")
        if state == "sealed":
            return
        if state != "open":
            raise RuntimeError("Failed local impersonation event receipt cannot seal")
        # The seal procedure finishes the lease in this transaction when this
        # was its last open source and the lease has already ended.
        conn.execute(
            "SELECT seal_impersonation_event_participant(%s,%s,'sealed',NULL,"
            "(SELECT count(*) FROM agent_impersonation_entries "
            "WHERE lease_id=%s AND source_key=%s))",
            (participant.lease_id, participant.source_key) * 2,
        )
        refresh_completed_export(conn, participant.lease_id)


def record_central_event(conn: psycopg.Connection, event: Event) -> Event:
    """Append one central audit event to its actor's open lease log, in the caller's transaction.

    Returns the event tagged with its session (or unchanged when no log is open).
    Callers emit the returned value to the observation sink only after their
    outer transaction commits; the record itself needs no emit to survive.
    """
    actor = _source_actor(event.source)
    if actor is None:
        return event
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT * FROM agent_impersonations WHERE agent_id=%s AND status='active' "
            "AND expires_at>clock_timestamp() AND automatic AND "
            "event_delivery_protocol_version=%s FOR UPDATE",
            (actor, LOG_PROTOCOL_VERSION),
        )
        leases = cur.fetchall()
    if not leases:
        return event
    if len(leases) != 1:
        raise RuntimeError("Central event admission found more than one active lease")
    lease = leases[0]
    if lease["event_admission_closed_at"] is not None:
        return event
    tagged = replace(
        event,
        attributes={
            **event.attributes,
            "impersonation_session": session_tag(actor, lease["session_id"]),
        },
    )
    # The body commits with the operation, under the lock that also closes admission.
    append_source_event(conn, lease, tagged, source_key=CENTRAL_SOURCE)
    return tagged


def emit_recorded_central_event(db: Database, event: Event) -> None:
    """Record a service-owned audit event, commit it, then emit those exact bytes.

    Services such as the computer daemon do not own the transaction that
    produced their operation; this gives them the same record -> commit -> emit
    order as transaction-owning producers: the actor's lease log (when one is
    open) and `audit_events` both take the tagged event in one short transaction.
    """
    from base import telemetry
    from base.telemetry.audit_events import record_audit

    with db.write_transaction() as conn:
        tagged = record_audit(conn, record_central_event(conn, event))
    telemetry.emit_prepared(tagged)


def _source_actor(source: str) -> int | None:
    if not source.startswith("agent:"):
        return None
    raw = source.removeprefix("agent:")
    if not raw.isdecimal():
        return None
    return int(raw)


class EventSourceNotSealedError(RuntimeError):
    """A participant is open or failed, so the lease cannot be released."""


def require_participants_sealed(conn: psycopg.Connection, lease: Mapping[str, Any]) -> None:
    """Refuse while any controller receipt is open or failed (release precondition)."""
    if not is_log_native(lease):
        return
    lease_id = lease["id"]
    source_keys = conn.execute(
        "SELECT source_key FROM agent_impersonation_event_participants WHERE lease_id=%s ORDER BY source_key",
        (lease_id,),
    ).fetchall()
    states = {locked_receipt_state(conn, lease_id, key) for (key,) in source_keys}
    if "failed" in states:
        raise EventSourceNotSealedError("Impersonation event capture failed")
    if "open" in states:
        raise EventSourceNotSealedError("Impersonation event participants have not sealed")


def close_event_admission(conn: psycopg.Connection, lease_id: str) -> bool:
    """Close a log-native lease's admission gate through its narrow SQL door."""
    row = conn.execute(
        "SELECT close_impersonation_event_admission(%s)",
        (lease_id,),
    ).fetchone()
    return row is not None and row[0] is True
