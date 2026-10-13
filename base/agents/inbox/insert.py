"""Inbound inserts: one inbox row, its lineage audit and the post-commit wake."""

import contextlib
import json

import psycopg

from base import telemetry
from base.agents.impersonation.manifest import record_central_event
from base.agents.impersonation.wake import publish_inbound_wake
from base.agents.messages.envelope import validate_source
from base.agents.messages.inbound_provenance import (
    InboundProvenance,
    content_sha256,
    source_assertion_match,
)
from base.db import Database, fetch_one
from base.events.caller_identity import caller_payload
from base.events.live.bus import EventBus
from base.telemetry import Event
from base.telemetry.audit_events import prepare_event_log, record_audit


def _reject_reserved_payload(payload: dict[str, object] | None) -> None:
    if payload is None:
        return
    if "lifecycle_result" in payload:
        raise ValueError("lifecycle_result is reserved for verified command settlement")
    if "launch_attempts" in payload:
        raise ValueError("launch_attempts is reserved for controller authorization")
    if "resurrection_retry" in payload:
        raise ValueError("resurrection_retry is reserved for the pending resurrection owner")
    if {"resurrection_launch", "resurrection_launch_attempts"} & payload.keys():
        raise ValueError("resurrection launch evidence is reserved for the lifecycle owner")


def _lineage_event(kind: str, source: str) -> tuple[str | None, int | None]:
    # Map inbound kind → lifecycle event_type. Only chat messages between
    # agents produce a 'send_message' event; user→agent chat is not an
    # inter-agent event. Lifecycle kinds map 1:1 except compact_summary /
    # compact_request which are handled elsewhere (agent self-insert /
    # insert_compact_request_inbound).
    _kind_to_event: dict[str, str | None] = {
        "chat": "send_message",
        "system_note": "send_message",
        "terminate": "terminate",
        "restart": "restart",
        "cancel": "cancel",
        "resurrect": "resurrect",
        "restart_completed": "restart_completed",
        "fork": "fork",
    }
    event_type = _kind_to_event.get(kind)
    # Parse the lineage parent from source. For inter-agent chat it is the
    # sender; for a kind='fork' lifecycle inbound the source is the fork
    # identity marker "agent:{fork_source}" — and per the fork-lineage ruling
    # (2026-08-28, task #1879) the fork event's target_agent_id must be the
    # fork SOURCE (the lineage parent), never the executor.
    target_agent_id: int | None = None
    if event_type == "send_message" and source.startswith("agent:"):
        with contextlib.suppress(ValueError):
            target_agent_id = int(source.removeprefix("agent:"))
    elif event_type == "send_message":
        # user/UI → agent chat is not an inter-agent event; skip event write
        event_type = None
    elif event_type == "fork" and source.startswith("agent:"):
        with contextlib.suppress(ValueError):
            target_agent_id = int(source.removeprefix("agent:"))
    return event_type, target_agent_id


def insert_inbound_message(
    db: psycopg.Connection,
    agent_id: int,
    content: str,
    source: str,
    kind: str = "chat",
    payload: dict[str, object] | None = None,
    provenance: InboundProvenance | None = None,
    *,
    database: Database,
    bus: EventBus,
) -> int:
    """UI / gateway call: INSERT one inbound; the agent's claim node
    fetches and dispatches.

    Args:
        source: provenance tag — claim node reads it and goes
            through `base/agents/messages/envelope.py:wrap_inbound` envelope prefix to tell
            the agent who the message came from. The UI passes
            `'user'`; a peer agent passes `'agent:N'`.
        kind: default `'chat'` (user dialogue). Other valid values
            see `db/schema.sql` CHECK constraint: `'compact_summary'`
            / `'compact_request'` / `'cancel'` / `'terminate'` / `'restart'`
            / `'restart_completed'` / `'resurrect'`. Non-'chat' usually
            pairs with `content=''` (control signal without payload).
        payload: optional JSONB sidecar. For a multimodal chat inbound this
            carries `{"content_blocks": [...]}` (the OpenAI-shaped text/image
            blocks); the claim node reads it to build a native multimodal
            HumanMessage while `content` holds the text part for legacy /
            envelope / timeline readers. None leaves the column NULL.

    Returns:
        The newly inserted inbound id — the caller can use it to
        publish an `inbound_arrived` event for the web UI to show in
        real time (spec §5).

    This is the self-committing shape: the row commits here, then the
    post-commit steps run. A writer that must keep several inbounds in one
    transaction calls :func:`insert_inbound_message_in_transaction` instead and
    owns its own commit and post-commit steps.
    """
    with db.cursor() as cur:
        new_id, prepared_event = insert_inbound_message_in_transaction(
            cur, agent_id, content, source, kind=kind, payload=payload, provenance=provenance
        )
    db.commit()
    emit_prepared_event(prepared_event)
    # Publish to Redis to wake the idle agent. Agents subscribe to
    # `<prefix>:inbound:{agent_id}` (inbound_channel) via RedisInboundListener.
    # Fire-and-forget: the agent's defensive SELECT recheck catches inbound
    # within timeout_s regardless — but a NOPERM is logged, not swallowed.
    publish_inbound_wake(database, bus, agent_id, str(new_id))
    return new_id


def insert_inbound_message_in_transaction(
    cur: psycopg.Cursor,
    agent_id: int,
    content: str,
    source: str,
    kind: str = "chat",
    payload: dict[str, object] | None = None,
    provenance: InboundProvenance | None = None,
) -> tuple[int, Event | None]:
    """INSERT one inbound in the caller's transaction; no commit, no wake.

    The batch shape of :func:`insert_inbound_message` (the pty closure notices
    write every notice of a sweep through it, in one transaction). The caller
    owns the outer transaction and the post-commit steps: it commits before any
    row is delivered, then emits the returned event — None for the shapes whose
    lineage has nothing to log — and wakes each owner via
    :func:`publish_inbound_wake`.
    """
    _reject_reserved_payload(payload)
    payload = caller_payload(source, payload)
    source_verified_by = provenance.source_verified_by if provenance is not None else None
    source_transport = provenance.source_transport if provenance is not None else None
    content_hash = content_sha256(content) if provenance is not None else None
    assertion_match = source_assertion_match(source, provenance) if provenance is not None else None
    event_type, target_agent_id = _lineage_event(kind, source)

    prepared_event = None
    cur.execute(
        "INSERT INTO inbound_messages "
        "(agent_id, content, kind, source, payload, source_verified_by, "
        "source_transport, content_hash, source_assertion_match) "
        "VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s) RETURNING id",
        (
            agent_id,
            content,
            kind,
            source,
            json.dumps(payload) if payload else None,
            source_verified_by,
            source_transport,
            content_hash,
            assertion_match,
        ),
    )
    new_id = fetch_one(cur, "insert inbound message")[0]
    if event_type is not None:
        prepared_event = prepare_event_log(
            event_type=event_type,
            agent_id=agent_id,
            source=source,
            target_agent_id=target_agent_id,
            payload={"inbound_id": new_id, "content": content}
            if content
            else {"inbound_id": new_id},
        )
        prepared_event = record_audit(
            cur.connection, record_central_event(cur.connection, prepared_event)
        )
    return new_id, prepared_event


def insert_spawn_prompt_in_transaction(
    cur: psycopg.Cursor, agent_id: int, content: str, source: str
) -> tuple[int, Event | None]:
    """Persist a spawn's first chat in the caller's row-creation transaction.

    This path deliberately does not commit or publish. An agent-sourced prompt
    is also recorded as a send_message audit fact in this transaction; the
    returned event is for the caller to hand to :func:`announce_spawn_prompt`
    after the commit. The pending scan covers a lost announcement. Spawn
    prompts have no multimodal payload or transport provenance; caller identity
    still follows the ordinary inbound rules.
    """
    validate_source(source)
    payload = caller_payload(source, None)
    cur.execute(
        "INSERT INTO inbound_messages (agent_id, content, kind, source, payload) "
        "VALUES (%s, %s, 'chat', %s, %s::jsonb) RETURNING id",
        (agent_id, content, source, json.dumps(payload) if payload else None),
    )
    inbound_id: int = fetch_one(cur, "insert spawn prompt")[0]
    if not source.startswith("agent:"):
        return inbound_id, None
    event = prepare_event_log(
        event_type="send_message",
        agent_id=agent_id,
        source=source,
        target_agent_id=int(source.removeprefix("agent:")),
        payload={"inbound_id": inbound_id, "content": content}
        if content
        else {"inbound_id": inbound_id},
    )
    return inbound_id, record_audit(cur.connection, record_central_event(cur.connection, event))


def announce_spawn_prompt(
    db: Database, bus: EventBus, agent_id: int, inbound_id: int, event: Event | None
) -> None:
    """Emit the recorded chat audit event and the wake hint after the prompt commits."""
    try:
        emit_prepared_event(event)
    finally:
        publish_inbound_wake(db, bus, agent_id, str(inbound_id))


def emit_prepared_event(event: Event | None) -> None:
    """Enqueue a transactional audit event only after its commit succeeded."""
    if event is not None:
        telemetry.emit_prepared(event)
