"""Agent creation identity committed with the birth row and initial prompt."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from uuid import UUID

import psycopg


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


def find_creation(conn: psycopg.Connection, key: str, request_hash: str) -> CreationReceipt | None:
    """Read a committed creation; later retries never insert another first prompt."""
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
    conn: psycopg.Connection, key: str | None, request_hash: str | None
) -> CreationReceipt | None:
    """Lock and recover a keyed birth inside its transaction; legacy births pass through."""
    if key is None and request_hash is None:
        return None
    if key is None or request_hash is None:
        raise ValueError("creation key and request hash must be supplied together")
    lock_creation(conn, key)
    return find_creation(conn, key, request_hash)
