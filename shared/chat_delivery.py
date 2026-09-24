"""Transactional identity for chat inbounds.

An HTTP response cache cannot close the interval between committing an inbound
and storing the response: a gateway death in that interval leaves the client
uncertain whether retrying will duplicate the message.  ``client_message_id``
therefore lives on the inbound row itself.  The unique claim, immutable-identity
check, and INSERT share one Postgres transaction; a retry returns the original
inbound id even when no HTTP response was ever stored.
"""

from __future__ import annotations

import contextlib
import json
from dataclasses import dataclass

import psycopg

from shared import telemetry
from shared.caller_identity import caller_payload
from shared.caller_protocol import require_caller_protocol
from shared.db import fetch_one, publish_inbound_wake
from shared.inbound_provenance import InboundProvenance, content_sha256, source_assertion_match


class ClientMessageConflictError(ValueError):
    """One client message id was reused for a different logical message."""


@dataclass(frozen=True, slots=True)
class ChatInboundReceipt:
    """The durable inbound identity and its recovery-relevant state."""

    inbound_id: int
    inserted: bool
    pending: bool


def _matching_receipt(
    row: tuple[object, ...],
    *,
    client_message_id: str,
    agent_id: int,
    content: str,
    source: str,
    payload: dict[str, object] | None,
) -> ChatInboundReceipt:
    (
        inbound_id,
        stored_agent,
        stored_content,
        stored_kind,
        stored_source,
        stored_payload,
        stored_status,
    ) = row
    expected_payload = payload or None
    mismatches = [
        name
        for name, actual, expected in (
            ("agent_id", stored_agent, agent_id),
            ("content", stored_content, content),
            ("kind", stored_kind, "chat"),
            ("source", stored_source, source),
            ("payload", stored_payload, expected_payload),
        )
        if actual != expected
    ]
    if mismatches:
        raise ClientMessageConflictError(
            f"client message id {client_message_id!r} already identifies a different "
            f"message ({', '.join(mismatches)})"
        )
    if not isinstance(inbound_id, int):
        raise TypeError(f"chat inbound id must be int, got {type(inbound_id).__name__}")
    return ChatInboundReceipt(
        inbound_id=inbound_id,
        inserted=False,
        pending=stored_status == "pending",
    )


def reconcile_chat_inbound(
    db: psycopg.Connection,
    *,
    client_message_id: str,
    agent_id: int,
    content: str,
    source: str,
    payload: dict[str, object] | None,
) -> ChatInboundReceipt | None:
    """Return the matching durable inbound, absent, or fail on key misuse."""
    payload = caller_payload(source, payload)
    with db.cursor() as cur:
        cur.execute(
            "SELECT id, agent_id, content, kind, source, payload, status "
            "FROM inbound_messages WHERE client_message_id = %s",
            (client_message_id,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return _matching_receipt(
        row,
        client_message_id=client_message_id,
        agent_id=agent_id,
        content=content,
        source=source,
        payload=payload,
    )


def insert_chat_inbound_once(
    db: psycopg.Connection,
    *,
    agent_id: int,
    content: str,
    source: str,
    payload: dict[str, object] | None,
    client_message_id: str | None,
    provenance: InboundProvenance | None = None,
) -> ChatInboundReceipt:
    """Insert one logical chat, or return its existing same-key inbound id."""
    with db.transaction():
        require_caller_protocol(db, agent_id, source)
        receipt, prepared_event = _insert_chat_inbound_once(
            db,
            agent_id=agent_id,
            content=content,
            source=source,
            payload=payload,
            client_message_id=client_message_id,
            provenance=provenance,
        )
    db.commit()
    if prepared_event is not None:
        telemetry.emit_prepared(prepared_event)
    if receipt.inserted:
        publish_inbound_wake(agent_id, str(receipt.inbound_id))
    return receipt


def _insert_chat_inbound_once(
    db: psycopg.Connection,
    *,
    agent_id: int,
    content: str,
    source: str,
    payload: dict[str, object] | None,
    client_message_id: str | None,
    provenance: InboundProvenance | None,
) -> tuple[ChatInboundReceipt, telemetry.Event | None]:
    """The locked INSERT body; ownership cannot change before commit."""
    payload = caller_payload(source, payload)
    encoded_payload = json.dumps(payload) if payload else None
    source_verified_by = provenance.source_verified_by if provenance is not None else None
    source_transport = provenance.source_transport if provenance is not None else None
    content_hash = content_sha256(content) if provenance is not None else None
    assertion_match = source_assertion_match(source, provenance) if provenance is not None else None
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO inbound_messages "
            "(agent_id, content, kind, source, payload, client_message_id, "
            "source_verified_by, source_transport, content_hash, source_assertion_match) "
            "VALUES (%s, %s, 'chat', %s, %s::jsonb, %s, %s, %s, %s, %s) "
            "ON CONFLICT (client_message_id) WHERE client_message_id IS NOT NULL "
            "DO NOTHING RETURNING id",
            (
                agent_id,
                content,
                source,
                encoded_payload,
                client_message_id,
                source_verified_by,
                source_transport,
                content_hash,
                assertion_match,
            ),
        )
        inserted = cur.fetchone()
        if inserted is None:
            if client_message_id is None:
                raise RuntimeError("a NULL client message id cannot conflict")
            cur.execute(
                "SELECT id, agent_id, content, kind, source, payload, status "
                "FROM inbound_messages WHERE client_message_id = %s",
                (client_message_id,),
            )
            receipt = _matching_receipt(
                fetch_one(cur, "select conflicting client message"),
                client_message_id=client_message_id,
                agent_id=agent_id,
                content=content,
                source=source,
                payload=payload,
            )
        else:
            receipt = ChatInboundReceipt(
                inbound_id=int(inserted[0]),
                inserted=True,
                pending=True,
            )

        prepared_event: telemetry.Event | None = None
        if receipt.inserted and source.startswith("agent:"):
            sender_id: int | None = None
            with contextlib.suppress(ValueError):
                sender_id = int(source.removeprefix("agent:"))
            prepared_event = telemetry.prepare_event(
                "audit",
                "send_message",
                agent_id=agent_id,
                source=source,
                target_agent_id=sender_id,
                attributes={"inbound_id": receipt.inbound_id, "content": content},
            )
            from shared.agents.impersonation_manifest import stage_central_expected_event

            prepared_event = stage_central_expected_event(
                db,
                prepared_event,
                origin_kind="chat_inbound",
                origin_id=receipt.inbound_id,
            )

    return receipt, prepared_event
