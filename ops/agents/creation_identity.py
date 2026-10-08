"""Agent creation identity committed with the birth row and initial prompt."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb


class CreationConflictError(ValueError):
    """A creation key already identifies a different request."""


@dataclass(frozen=True, slots=True)
class CreationReceipt:
    agent_id: int
    machine: str
    config: dict[str, object] | None
    birth_config: dict[str, object]
    launch_attempt_id: UUID
    launch_pending: bool


def creation_request_hash(request: dict[str, object]) -> str:
    """Hash the immutable request before preset or fork resolution mutates it."""
    encoded = json.dumps(request, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode()).hexdigest()


def lock_creation(conn: psycopg.Connection, key: str) -> None:
    """Serialize same-key births until the birth transaction commits or rolls back."""
    conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (key,))


def find_creation(
    conn: psycopg.Connection, key: str, request_hash: str, *, immutable_snapshot: bool = False
) -> CreationReceipt | None:
    """Read a committed creation; later retries never insert another first prompt."""
    if immutable_snapshot:
        return _find_snapshot(conn, key, request_hash)
    row = conn.execute(
        "SELECT id, creation_request_hash, machine, config_overlay, birth_config, "
        "last_launch_attempt_id, status='idling' AND last_admission_at IS NULL "
        "FROM agents_meta WHERE creation_key=%s",
        (key,),
    ).fetchone()
    if row is None:
        return None
    if row[1] != request_hash:
        raise CreationConflictError("creation key already identifies a different request")
    return CreationReceipt(row[0], row[2], row[3], row[4], row[5], row[6])


def recover_birth(
    conn: psycopg.Connection,
    key: str | None,
    request_hash: str | None,
    *,
    immutable_snapshot: bool = False,
) -> CreationReceipt | None:
    """Lock and recover a keyed birth inside its transaction; legacy births pass through."""
    if key is None and request_hash is None:
        return None
    if key is None or request_hash is None:
        raise ValueError("creation key and request hash must be supplied together")
    lock_creation(conn, key)
    return find_creation(conn, key, request_hash, immutable_snapshot=immutable_snapshot)


def _find_snapshot(conn: psycopg.Connection, key: str, request_hash: str) -> CreationReceipt | None:
    row = conn.execute(
        "SELECT s.agent_id, s.request_hash, s.machine, s.config_overlay, s.birth_config, "
        "s.launch_attempt_id, COALESCE(m.last_launch_attempt_id=s.launch_attempt_id "
        "AND m.machine=s.machine AND m.status='idling' AND m.last_admission_at IS NULL, FALSE) "
        "FROM agent_creation_snapshots s LEFT JOIN agents_meta m ON m.id=s.agent_id "
        "WHERE s.creation_key=%s",
        (key,),
    ).fetchone()
    if row is None:
        if conn.execute("SELECT 1 FROM agents_meta WHERE creation_key=%s", (key,)).fetchone():
            raise CreationConflictError("creation snapshot is unavailable for this key")
        return None
    if row[1] != request_hash:
        raise CreationConflictError("creation key already identifies a different request")
    return CreationReceipt(row[0], row[2], row[3], row[4], row[5], row[6])


def record_creation_snapshot(
    conn: psycopg.Connection,
    *,
    key: str,
    request_hash: str,
    agent_id: int,
    machine: str,
    config: dict[str, object] | None,
    birth_config: dict[str, object],
    launch_attempt_id: UUID,
    prompt_inbound_id: int | None,
    prompt_content: str | None,
    prompt_source: str | None,
) -> None:
    """Retain the first guarded draft birth in its caller-owned transaction."""
    conn.execute(
        "INSERT INTO agent_creation_snapshots "
        "(creation_key, request_hash, agent_id, machine, config_overlay, birth_config, "
        "launch_attempt_id, prompt_inbound_id, prompt_content, prompt_source) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (
            key,
            request_hash,
            agent_id,
            machine,
            Jsonb(config),
            Jsonb(birth_config),
            launch_attempt_id,
            prompt_inbound_id,
            prompt_content,
            prompt_source,
        ),
    )
