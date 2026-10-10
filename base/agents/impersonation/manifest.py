"""Local producer receipts and central staging for impersonation event logs.

A controller's SDK events are appended to the lease's log at the capture seam,
before the telemetry queue, while its receipt is open; transactional central
audit events are appended in their producing transaction. The receipts and the
completeness predicate live in the database (`event_log`).
"""

from __future__ import annotations

from collections.abc import Callable, Generator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from threading import Condition
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
from base.agents.messages.delivery.retry import retryable_database_error
from base.agents.sdk.capture import SdkCaptureAdmission
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
class LocalCaptureGate:
    """Per-participant close fence for local producer work."""

    participant: LocalParticipant
    admission_closed: bool = False
    in_flight: int = 0
    capture_failure_pending: bool = False
    condition: Condition = field(default_factory=Condition, repr=False)

    def admit(self, *, fail_closed_capture: bool = False) -> LocalCaptureAdmission | None:
        with self.condition:
            if self.admission_closed:
                if fail_closed_capture:
                    self.capture_failure_pending = True
                return None
            self.in_flight += 1
        return LocalCaptureAdmission(self)

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
class LocalCaptureAdmission:
    """One admitted SDK call that may still emit after close starts."""

    gate: LocalCaptureGate
    released: bool = False

    def capture_failed(self) -> None:
        """Fail this call's original receipt before its admission can drain."""
        _mark_participant_failed(self.gate.participant, gate=self.gate)

    def capture(self, event: Event) -> Event:
        """Capture this call's event against its retained original receipt."""
        if self.released:
            raise RuntimeError("SDK capture admission is already released")
        return capture_local_event(event, admission=self)

    def release(self) -> None:
        if self.released:
            return
        self.released = True
        _release_capture_admission(self.gate)


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


@contextmanager
def admitted_local_sdk_call(
    admit: Callable[[], SdkCaptureAdmission | None] | None = None,
) -> Generator[SdkCaptureAdmission | None, None, None]:
    """Retain and release the original call admission supplied by its SDK owner."""
    admission = None if admit is None else admit()
    primary: BaseException | None = None
    try:
        yield admission
    except BaseException as exc:
        primary = exc
        raise
    finally:
        if admission is not None:
            try:
                admission.release()
            except BaseException as secondary:
                if primary is None:
                    raise
                primary.add_note(f"SDK capture admission release also failed: {secondary!r}")


def begin_local_participant_close(gate: LocalCaptureGate, mark_closing: Callable[[], None]) -> None:
    """Atomically begin attachment close and fence new local SDK admissions."""
    gate.begin_close(mark_closing)


def close_local_participant_admission(gate: LocalCaptureGate, *, timeout: float) -> bool:
    """Close new local admissions and bounded-wait for already admitted work."""
    return gate.close_and_wait(timeout)


def _release_capture_admission(gate: LocalCaptureGate) -> None:
    should_seal = False
    with gate.condition:
        if gate.in_flight <= 0:
            raise RuntimeError("Impersonation event capture admission underflow")
        gate.in_flight -= 1
        gate.condition.notify_all()
        should_seal = gate.admission_closed and gate.in_flight == 0
    if should_seal:
        try:
            seal_local_participant(gate)
        except (OSError, EventSourceNotSealedError):
            logger.exception("Could not seal drained impersonation event receipt")
        except Exception as exc:
            if not retryable_database_error(exc):
                raise
            logger.exception("Could not seal drained impersonation event receipt")


def _is_local_eligible(event: Event, participant: LocalParticipant) -> bool:
    if event.event_name == "sdk_call":
        return event.agent_id == participant.agent_id
    return event.category == "audit" and (
        event.source == f"agent:{participant.agent_id}"
        or (event.source == "self" and event.agent_id == participant.agent_id)
    )


def capture_local_event(
    event: Event,
    *,
    gate: LocalCaptureGate | None = None,
    admission: LocalCaptureAdmission | None = None,
) -> Event:
    """Tag and record an eligible local event before the telemetry queue.

    A writer failure marks the receipt failed whenever the database is
    reachable, leaving the lease honestly pending instead of making a
    best-effort sink failure look complete. A direct event after closure is
    refused because it cannot be added to the sealed receipt.
    """
    if admission is not None:
        if gate is not None and gate is not admission.gate:
            raise ValueError("Capture admission belongs to another gate")
        gate = admission.gate
    if gate is None or not _is_local_eligible(event, gate.participant):
        return event
    participant = gate.participant
    transient_admission: LocalCaptureAdmission | None = None
    if admission is None:
        transient_admission = gate.admit(fail_closed_capture=True)
        if transient_admission is None:
            _mark_participant_failed(participant, gate=gate)
            raise RuntimeError("Impersonation event capture is closed")
    tagged = replace(
        event,
        attributes={
            **event.attributes,
            "impersonation_session": session_tag(participant.agent_id, participant.session_id),
        },
    )
    primary: BaseException | None = None
    try:
        _write_local_capture(gate, tagged)
    except BaseException as exc:
        primary = exc
        raise
    finally:
        if transient_admission is not None:
            try:
                transient_admission.release()
            except BaseException as secondary:
                if primary is None:
                    raise
                primary.add_note(f"Local capture admission release also failed: {secondary!r}")
    return tagged


def _write_local_capture(gate: LocalCaptureGate, event: Event) -> None:
    """Keep known writer availability recovery separate from unknown producer errors."""
    try:
        _insert_local_item(gate.participant, event)
    except BaseException as primary:
        failure_recording_failed = False
        try:
            _mark_participant_failed(gate.participant, gate=gate)
        except BaseException as secondary:
            primary.add_note(f"Receipt failure recording also failed: {secondary!r}")
            failure_recording_failed = True
        if (
            failure_recording_failed
            or not isinstance(primary, Exception)
            or not retryable_database_error(primary)
        ):
            raise


def _insert_local_item(participant: LocalParticipant, event: Event) -> None:
    with participant.db.write_transaction() as conn:
        lease = lock_lease(conn, participant.lease_id)
        if not is_log_native(lease):
            raise RuntimeError("Local receipt belongs to a lease without an event log")
        state = locked_receipt_state(conn, participant.lease_id, participant.source_key)
        if state != "open":
            raise RuntimeError("Local receipt is not open for event capture")
        append_source_event(conn, lease, event, source_key=participant.source_key)


def _mark_participant_failed(
    participant: LocalParticipant, *, gate: LocalCaptureGate | None = None
) -> None:
    """Record a capture failure without allowing a transient writer loss to erase it."""
    if gate is not None:
        with gate.condition:
            gate.capture_failure_pending = True
    try:
        _persist_capture_failure(participant)
    except Exception as exc:
        if not retryable_database_error(exc):
            raise
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


def seal_local_participant(gate: LocalCaptureGate) -> None:
    """Seal one receipt after its controller has finished admitted work."""
    if not gate.close_and_wait(0):
        raise RuntimeError("Local receipt still has admitted SDK calls")
    participant = gate.participant
    with gate.condition:
        retry_capture_failure = gate.capture_failure_pending
    if retry_capture_failure:
        _mark_participant_failed(participant, gate=gate)
        with gate.condition:
            if gate.capture_failure_pending:
                raise EventSourceNotSealedError("Local capture failure is not durably recorded")
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
            raise EventSourceNotSealedError("Failed local impersonation event receipt cannot seal")
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
