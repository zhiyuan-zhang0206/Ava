"""Protocol-v1 producer receipts for impersonation event delivery.

The manifest is an upstream census, not a delivery acknowledgement.  Local
controller events are captured before telemetry enqueue; transactional central
audit events are staged before their transaction commits.  Only the owning
agent-host may later certify the frozen union against central telemetry and the
durable handoff ledger.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Generator
from contextlib import contextmanager
from contextvars import ContextVar  # noqa: TID251 -- SDK async finally needs task-local admission
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from threading import Condition, Lock
from typing import Any

import psycopg
from psycopg.rows import dict_row

from shared._impersonation_store import ImpersonationError, lock_lease
from shared.config import settings
from shared.db_transaction import write_transaction
from shared.log import logger
from shared.telemetry import Event, event_id, event_line, event_line_digest

PROTOCOL_VERSION = 1
_PENDING_REASONS = frozenset(
    {
        "awaiting_session_end",
        "awaiting_participant_seal",
        "capture_failed",
        "awaiting_indexed_ids",
        "manifest_mismatch",
        "retention_loss",
    }
)


@dataclass(frozen=True)
class LocalParticipant:
    """The one controller process that may capture this process's events."""

    lease_id: str
    agent_id: int
    session_id: int
    source_key: str


@dataclass
class _CaptureGate:
    """Per-participant close fence for local producer work."""

    participant: LocalParticipant
    admission_closed: bool = False
    in_flight: int = 0
    capture_failure_pending: bool = False
    condition: Condition = field(default_factory=Condition, repr=False)

    def admit(self) -> _CaptureAdmission | None:
        with self.condition:
            if self.admission_closed:
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
    "impersonation_manifest_sdk_capture_admission", default=None
)


def session_tag(agent_id: int, session_id: int) -> str:
    """Return the immutable correlation value shared by receipts and Loki."""
    return f"{agent_id}:{session_id}"


def is_protocol_v1(lease: dict[str, Any]) -> bool:
    """Whether this lease is admitted to the manifest protocol."""
    return (
        lease["automatic"] is True and lease["event_delivery_protocol_version"] == PROTOCOL_VERSION
    )


def admit_certifier(conn: psycopg.Connection, lease_id: str) -> None:
    """Bind the target native host's secret in its acceptance transaction."""
    secret = settings.general.impersonation_event_manifest_certification_secret
    if len(secret) < 32:
        raise ImpersonationError("Manifest acceptance requires a certification secret")
    conn.execute("SELECT admit_impersonation_event_certifier(%s,%s)", (lease_id, secret))


def pending_reason(lease: dict[str, Any]) -> str | None:
    """Classify a handoff's current delivery uncertainty without inventing state."""
    if lease["events_completed_at"] is not None:
        return None
    if lease["automatic"] is not True:
        return "manual"
    if lease["event_delivery_protocol_version"] is None:
        return "legacy"
    stored = lease["event_delivery_pending_reason"]
    if stored is not None:
        return str(stored)
    if lease["ended_at"] is None:
        return "awaiting_session_end"
    if lease["manifest_frozen_at"] is None:
        return "awaiting_participant_seal"
    return "awaiting_indexed_ids"


def open_local_participant(lease_id: str, *, agent_id: int, source_key: str) -> bool:
    """Open one controller receipt before it can emit an eligible event."""
    with write_transaction() as conn:
        lease = lock_lease(conn, lease_id)
        if not is_protocol_v1(lease):
            return False
        if lease["agent_id"] != agent_id:
            raise RuntimeError("Local receipt belongs to another agent")
        if lease["manifest_admission_closed_at"] is not None:
            raise RuntimeError("Impersonation event-manifest admission is closed")
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
    """Whether this SDK call crossed the manifest gate before close started."""
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


def _event_item(event: Event) -> tuple[str, str, str, object]:
    line = event_line(event)
    timestamp_ns = int(event.ts.timestamp() * 1_000_000_000)
    key = f"event:{event_id(line, timestamp_ns)}"
    kind = "sdk_call" if event.event_name == "sdk_call" else "api_event"
    return key, event_line_digest(event), kind, event.ts


def capture_local_event(event: Event) -> Event:
    """Tag and record an eligible local event before the telemetry queue.

    Capture failure never suppresses ordinary telemetry.  It marks the receipt
    failed whenever the database is reachable, leaving the lease honestly
    pending instead of making a best-effort sink failure look complete.
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
        transient_admission = gate.admit()
        if transient_admission is None:
            return event
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
        logger.exception("Impersonation event-manifest local capture failed")
        _mark_participant_failed(participant)
    finally:
        if transient_admission is not None:
            transient_admission.release()
    return tagged


def _insert_local_item(participant: LocalParticipant, event: Event) -> None:
    key, digest, kind, timestamp = _event_item(event)
    with write_transaction() as conn:
        lease = lock_lease(conn, participant.lease_id)
        if not is_protocol_v1(lease):
            raise RuntimeError("Local receipt belongs to a lease outside protocol v1")
        row = conn.execute(
            "SELECT state FROM agent_impersonation_event_participants "
            "WHERE lease_id=%s AND source_key=%s FOR UPDATE",
            (participant.lease_id, participant.source_key),
        ).fetchone()
        if row is None or row[0] != "open":
            raise RuntimeError("Local receipt is not open for event capture")
        item_count = conn.execute(
            "SELECT count(*) FROM agent_impersonation_event_participant_items "
            "WHERE lease_id=%s AND source_key=%s",
            (participant.lease_id, participant.source_key),
        ).fetchone()
        if (
            item_count is None
            or int(item_count[0]) >= settings.general.impersonation_event_manifest_max_items
        ):
            raise RuntimeError("Impersonation event manifest reached its configured item cap")
        inserted = conn.execute(
            "INSERT INTO agent_impersonation_event_participant_items("
            "lease_id,source_key,event_key,event_kind,event_at,line_sha256) "
            "VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT (lease_id,source_key,event_key) "
            "DO NOTHING RETURNING line_sha256",
            (participant.lease_id, participant.source_key, key, kind, timestamp, digest),
        ).fetchone()
        if inserted is None:
            existing = conn.execute(
                "SELECT line_sha256 FROM agent_impersonation_event_participant_items "
                "WHERE lease_id=%s AND source_key=%s AND event_key=%s",
                (participant.lease_id, participant.source_key, key),
            ).fetchone()
            if existing is None or existing[0] != digest:
                raise RuntimeError("Impersonation event id maps to conflicting event bytes")


def _mark_participant_failed(participant: LocalParticipant) -> None:
    """Record a capture failure without allowing a transient writer loss to erase it."""
    gate = _capture_gate(participant)
    if gate is not None:
        with gate.condition:
            gate.capture_failure_pending = True
    try:
        _persist_capture_failure(participant)
    except Exception:
        logger.exception("Could not record impersonation event-manifest capture failure")
        return
    if gate is not None:
        with gate.condition:
            gate.capture_failure_pending = False
    _alert_capture_failure(participant)


def _persist_capture_failure(participant: LocalParticipant) -> None:
    """Durably turn an open receipt into failed before alert delivery is attempted."""
    with write_transaction() as conn:
        lease = lock_lease(conn, participant.lease_id)
        receipt = conn.execute(
            "SELECT state FROM agent_impersonation_event_participants "
            "WHERE lease_id=%s AND source_key=%s FOR UPDATE",
            (participant.lease_id, participant.source_key),
        ).fetchone()
        if receipt is None:
            raise RuntimeError("Missing local impersonation event receipt")
        if receipt[0] == "open":
            conn.execute(
                "SELECT seal_impersonation_event_participant(%s,%s,'failed','capture_failed',NULL,NULL)",
                (participant.lease_id, participant.source_key),
            )
            conn.execute(
                "UPDATE agent_impersonations SET event_delivery_pending_reason='capture_failed' "
                "WHERE id=%s AND events_completed_at IS NULL",
                (participant.lease_id,),
            )
        elif receipt[0] != "failed":
            raise RuntimeError("Cannot record a capture failure after receipt sealing")
        # Keep ``lease`` live so its dict-row shape is checked before alerting.
        if str(lease["id"]) != participant.lease_id:
            raise RuntimeError("Local receipt belongs to another lease")


def _alert_capture_failure(participant: LocalParticipant) -> None:
    """Best-effort alert after the failed receipt is committed independently."""
    try:
        with write_transaction() as conn:
            lease = lock_lease(conn, participant.lease_id)
            _upsert_manifest_alert(
                _alerts_upsert(),
                conn,
                lease,
                "ImpersonationManifestCaptureFailed",
                datetime.now(UTC),
            )
    except Exception:
        logger.exception("Could not alert on impersonation event-manifest capture failure")


def _alerts_upsert() -> Any:
    from shared.alerts import upsert_alert

    return upsert_alert


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
    with write_transaction() as conn:
        lease = lock_lease(conn, participant.lease_id)
        if not is_protocol_v1(lease):
            return
        receipt = conn.execute(
            "SELECT state FROM agent_impersonation_event_participants "
            "WHERE lease_id=%s AND source_key=%s FOR UPDATE",
            (participant.lease_id, participant.source_key),
        ).fetchone()
        if receipt is None:
            raise RuntimeError("Missing local impersonation event receipt")
        if receipt[0] == "sealed":
            return
        if receipt[0] != "open":
            raise RuntimeError("Failed local impersonation event receipt cannot seal")
        digest, count = _participant_digest(conn, participant.lease_id, participant.source_key)
        conn.execute(
            "SELECT seal_impersonation_event_participant(%s,%s,'sealed',NULL,%s,%s)",
            (participant.lease_id, participant.source_key, count, digest),
        )


def _participant_digest(
    conn: psycopg.Connection, lease_id: str, source_key: str
) -> tuple[str, int]:
    rows = conn.execute(
        "SELECT event_key,line_sha256,event_kind FROM agent_impersonation_event_participant_items "
        "WHERE lease_id=%s AND source_key=%s ORDER BY event_key",
        (lease_id, source_key),
    ).fetchall()
    encoded = "\n".join(f"{key}:{digest}:{kind}" for key, digest, kind in rows).encode()
    return hashlib.sha256(encoded).hexdigest(), len(rows)


def stage_central_expected_event(
    conn: psycopg.Connection,
    event: Event,
    *,
    origin_kind: str,
    origin_id: int,
) -> Event:
    """Stage one central audit event in its producer transaction.

    The exact prepared event is returned with its session tag.  Callers emit
    that returned value only after their outer transaction commits.
    """
    actor = _source_actor(event.source)
    if actor is None:
        return event
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT * FROM agent_impersonations WHERE agent_id=%s AND status='active' "
            "AND expires_at>clock_timestamp() AND automatic AND "
            "event_delivery_protocol_version=%s FOR UPDATE",
            (actor, PROTOCOL_VERSION),
        )
        leases = cur.fetchall()
    if not leases:
        return event
    if len(leases) != 1:
        raise RuntimeError("Central event admission found more than one active manifest lease")
    lease = leases[0]
    if lease["manifest_admission_closed_at"] is not None:
        return event
    tagged = replace(
        event,
        attributes={
            **event.attributes,
            "impersonation_session": session_tag(actor, lease["session_id"]),
        },
    )
    key, digest, kind, timestamp = _event_item(tagged)
    conn.execute(
        "INSERT INTO agent_impersonation_event_expected_receipts(lease_id,origin_kind,origin_id) "
        "VALUES(%s,%s,%s) ON CONFLICT (lease_id,origin_kind,origin_id) DO NOTHING",
        (lease["id"], origin_kind, origin_id),
    )
    existing = conn.execute(
        "SELECT line_sha256 FROM agent_impersonation_event_expected_items "
        "WHERE lease_id=%s AND event_key=%s",
        (lease["id"], key),
    ).fetchone()
    if existing is not None:
        if existing[0] != digest:
            raise RuntimeError("Central expected event id maps to conflicting event bytes")
        return tagged
    conn.execute(
        "INSERT INTO agent_impersonation_event_expected_items("
        "lease_id,event_key,event_kind,event_at,line_sha256,origin_kind,origin_id) "
        "VALUES(%s,%s,%s,%s,%s,%s,%s)",
        (lease["id"], key, kind, timestamp, digest, origin_kind, origin_id),
    )
    return tagged


def emit_staged_central_event(event: Event, *, origin_kind: str, origin_id: int) -> None:
    """Stage a service-owned audit event, commit it, then enqueue those exact bytes.

    Services such as the computer daemon do not own the transaction that
    produced their operation.  They still must not enqueue a v1 audit fact
    until the matching expected receipt is durable, so this helper gives them
    the same stage -> commit -> emit order as transaction-owning producers.
    ``origin_id`` is the producer's durable request/action identity and keeps
    redelivery idempotent alongside the immutable event-key check.
    """
    with write_transaction() as conn:
        tagged = stage_central_expected_event(
            conn, event, origin_kind=origin_kind, origin_id=origin_id
        )
    from shared import telemetry

    telemetry.emit_prepared(tagged)


def _source_actor(source: str) -> int | None:
    if not source.startswith("agent:"):
        return None
    raw = source.removeprefix("agent:")
    if not raw.isdecimal():
        return None
    return int(raw)


def freeze_manifest(conn: psycopg.Connection, lease: dict[str, Any]) -> None:
    """Freeze the sealed local and central union immediately before release."""
    if not is_protocol_v1(lease):
        return
    receipts = conn.execute(
        "SELECT state FROM agent_impersonation_event_participants WHERE lease_id=%s FOR UPDATE",
        (lease["id"],),
    ).fetchall()
    states = {row[0] for row in receipts}
    if "failed" in states:
        raise RuntimeError("Impersonation event capture failed")
    if "open" in states:
        raise RuntimeError("Impersonation event participants have not sealed")
    items = frozen_items(conn, str(lease["id"]))
    digest = _aggregate_digest(items)
    floor = lease["activated_at"]
    if floor is None:
        raise RuntimeError("Manifest leases require activation before release")
    conn.execute(
        "SELECT freeze_impersonation_event_manifest(%s,%s,%s,%s)",
        (lease["id"], digest, len(items), floor - _skew_guard()),
    )


def close_manifest_admission(conn: psycopg.Connection, lease_id: str) -> bool:
    """Close a protocol-v1 lease's admission gate through its narrow SQL door."""
    row = conn.execute(
        "SELECT close_impersonation_event_manifest_admission(%s)",
        (lease_id,),
    ).fetchone()
    return row is not None and row[0] is True


def frozen_items(conn: psycopg.Connection, lease_id: str) -> dict[str, tuple[str, str]]:
    """Return the frozen expected union, rejecting equal ids with different bytes."""
    rows = conn.execute(
        "SELECT event_key,line_sha256,event_kind FROM ("
        "SELECT event_key,line_sha256,event_kind FROM agent_impersonation_event_participant_items "
        "WHERE lease_id=%s UNION ALL "
        "SELECT event_key,line_sha256,event_kind FROM agent_impersonation_event_expected_items "
        "WHERE lease_id=%s) expected ORDER BY event_key",
        (lease_id, lease_id),
    ).fetchall()
    items: dict[str, tuple[str, str]] = {}
    for key, digest, kind in rows:
        previous = items.setdefault(key, (digest, kind))
        if previous != (digest, kind):
            raise RuntimeError("Manifest event key has conflicting bytes or kind")
    return items


def _aggregate_digest(items: dict[str, tuple[str, str]]) -> str:
    material = "\n".join(
        f"{key}:{digest}:{kind}" for key, (digest, kind) in sorted(items.items())
    ).encode()
    return hashlib.sha256(material).hexdigest()


def _skew_guard() -> timedelta:
    return timedelta(seconds=settings.general.impersonation_event_clock_skew_guard_seconds)


def set_pending_reason(lease_id: str, reason: str) -> None:
    """Persist one precise retry diagnostic without adding a third state."""
    if reason not in _PENDING_REASONS:
        raise ValueError(f"Unknown impersonation event pending reason {reason!r}")
    with write_transaction() as conn:
        conn.execute(
            "UPDATE agent_impersonations SET event_delivery_pending_reason=%s "
            "WHERE id=%s AND events_completed_at IS NULL",
            (reason, lease_id),
        )


def monitor_manifest_health(*, machine: str) -> None:
    """Persist operator alerts for aged, slow, or retention-lost local leases.

    This is diagnostic-only: it never changes a pending manifest to complete
    and a live receipt remains open until it seals or reports a real failure.
    """
    from shared.alerts import upsert_alert
    from shared.loki_index_labels import retention_floor

    now = datetime.now(UTC)
    horizon = retention_floor(now)
    with write_transaction() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT * FROM agent_impersonations WHERE machine=%s AND automatic "
            "AND event_delivery_protocol_version=%s AND events_completed_at IS NULL",
            (machine, PROTOCOL_VERSION),
        )
        rows = cur.fetchall()
        for lease in rows:
            _monitor_one_manifest(conn, upsert_alert, lease, now=now, horizon=horizon)


def _monitor_one_manifest(
    conn: psycopg.Connection,
    upsert_alert: Any,
    lease: dict[str, Any],
    *,
    now: datetime,
    horizon: datetime,
) -> None:
    if _retention_lost(lease, horizon=horizon):
        conn.execute(
            "SELECT record_impersonation_event_retention_loss(%s,%s)",
            (lease["id"], horizon),
        )
        _upsert_manifest_alert(upsert_alert, conn, lease, "ImpersonationEventRetentionLoss", now)
        return
    if _has_slow_open_participant(conn, str(lease["id"]), now=now):
        _upsert_manifest_alert(upsert_alert, conn, lease, "ImpersonationManifestSealSlow", now)
        return
    if _is_old_pending(lease, now=now):
        _upsert_manifest_alert(upsert_alert, conn, lease, "ImpersonationEventDeliveryPending", now)


def _retention_lost(lease: dict[str, Any], *, horizon: datetime) -> bool:
    return (
        lease["manifest_frozen_at"] is not None
        and lease["manifest_envelope_floor_at"] < horizon
        and lease["events_completed_at"] is None
    )


def _has_slow_open_participant(conn: psycopg.Connection, lease_id: str, *, now: datetime) -> bool:
    row = conn.execute(
        "SELECT 1 FROM agent_impersonation_event_participants WHERE lease_id=%s "
        "AND state='open' AND opened_at<%s LIMIT 1",
        (
            lease_id,
            now
            - timedelta(seconds=settings.general.impersonation_event_manifest_seal_wait_seconds),
        ),
    ).fetchone()
    return row is not None


def alert_if_participant_still_open(participant: LocalParticipant) -> None:
    """Emit the detach-wait alert without changing a live participant's state.

    An attachment can remain in a held SDK ``finally`` past the configured
    detach wait.  That is evidence for an operator, not permission to call a
    live producer failed or to freeze an empty manifest.
    """
    now = datetime.now(UTC)
    with write_transaction() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT l.* FROM agent_impersonations l JOIN "
            "agent_impersonation_event_participants p ON p.lease_id=l.id "
            "WHERE p.lease_id=%s AND p.source_key=%s AND p.state='open'",
            (participant.lease_id, participant.source_key),
        )
        lease = cur.fetchone()
        if lease is not None:
            _upsert_manifest_alert(
                _alerts_upsert(), conn, lease, "ImpersonationManifestSealSlow", now
            )


def _is_old_pending(lease: dict[str, Any], *, now: datetime) -> bool:
    ended = lease["ended_at"]
    return ended is not None and ended < now - timedelta(
        seconds=settings.general.impersonation_event_delivery_alert_age_seconds
    )


def _upsert_manifest_alert(
    upsert_alert: Any,
    conn: psycopg.Connection,
    lease: dict[str, Any],
    alertname: str,
    now: datetime,
) -> None:
    upsert_alert(
        conn,
        {
            "status": "firing",
            "labels": {
                "alertname": alertname,
                "severity": "warning",
                "lease_id": str(lease["id"]),
                "machine": str(lease["machine"]),
            },
            "annotations": {
                "pending_reason": str(lease["event_delivery_pending_reason"] or "unknown"),
                "session": f"{lease['agent_id']}:{lease['session_id']}",
            },
            "starts_at": now.isoformat(),
        },
        source="machine-probe",
    )


def retention_loss_panel(*, machine: str) -> list[dict[str, Any]]:
    """Read the retention-loss panel rows for operator diagnostics."""
    with write_transaction() as conn:
        rows = conn.execute(
            "SELECT l.id,l.agent_id,l.session_id,l.manifest_envelope_floor_at,"
            "l.event_delivery_retention_horizon_at,l.created_at,"
            "GREATEST(0,l.manifest_item_count-(SELECT count(*) FROM agent_impersonation_entries e "
            "WHERE e.lease_id=l.id AND e.kind IN ('sdk_call','api_event'))) AS missing_item_count "
            "FROM agent_impersonations l "
            "WHERE l.machine=%s AND l.event_delivery_pending_reason='retention_loss' "
            "ORDER BY l.created_at",
            (machine,),
        ).fetchall()
    return [
        {
            "lease_id": str(row[0]),
            "agent_id": row[1],
            "session_id": row[2],
            "envelope_floor_at": row[3],
            "retention_horizon_at": row[4],
            "created_at": row[5],
            "missing_item_count": row[6],
        }
        for row in rows
    ]


def certify(lease_id: str) -> bool:
    """Invoke certification with this host's non-exported runner proof."""
    from psycopg.types.json import Jsonb

    from shared.impersonation_history import export_handoff

    with write_transaction() as conn:
        row = conn.execute(
            "SELECT certify_impersonation_event_delivery(%s,%s)",
            (lease_id, settings.general.impersonation_event_manifest_certification_secret),
        ).fetchone()
        if row is None or row[0] is not True:
            return False
        lease = lock_lease(conn, lease_id)
        if lease["handoff_path"] is not None:
            document, _ = export_handoff(lease, conn)
            conn.execute(
                "UPDATE agent_impersonations SET handoff_document=%s WHERE id=%s",
                (Jsonb(document), lease_id),
            )
    return True
