"""Durable close-notice outbox for persistent shells interrupted by `ava stop` (issue #2044).

`ava stop` closes busy persistent-shell sessions AFTER the gateway and ops
server are already down, so the closure notice for each owner agent cannot be
delivered synchronously. The stop path records one notice per busy session it
VERIFIED closed (exact process identity gone, no terminals left) under
``$AVA_HOME/state/pty-close-notices/`` — durable across the data-plane
shutdown. The ops daemon flushes the journal at its next startup: a notice for
a live owner becomes a system inbound message, one for a terminated/restarting
owner is dropped without delivery (a closure notice must never resurrect a dead
agent — the TTL reaper's boundary, gateway/ttl_reaper.py:83).

One file per (machine, agent_id, session_id, shell-birth) dedup key: a stop
retry or a CLI re-entry overwrites the same record instead of stacking a
duplicate notice (issue #2044 acceptance #4). Delivery is exactly-once even
across a crash between the DB commit and the record deletion — the
`api_idempotency` claim row settles the re-flush.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from psycopg_pool import ConnectionPool

from ops.cluster_status import _AGENT_SHELL_RE
from shared.db import insert_inbound_message, publish_inbound_wake
from shared.db_transaction import write_transaction
from shared.inbound_provenance import InboundProvenance
from shared.log import logger
from shared.paths import ava_home

# The reaper's notifiable boundary: only these statuses receive a closure
# notice; anything else (terminated / restarting / missing) drops the record.
_NOTIFIABLE_STATUSES = ("running", "idling")

# The only caller that reaches terminal closure through this journal is the
# operator's `ava stop` (updates and pause retain terminals; see
# cli/commands/_temporary_stop.stop).
_REASON = "an operator stop (ava stop)"


@dataclass(frozen=True)
class ClosureNotice:
    """One verified-closed busy session and the stop that closed it."""

    machine: str
    agent_id: int
    session_id: int
    name: str
    shell_pid: int
    shell_birth: str
    operation: str
    acquired_at: str
    reason: str
    closed_at: str

    def dedup_key(self) -> str:
        raw = f"{self.machine}|{self.agent_id}|{self.session_id}|{self.shell_birth}"
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    def as_dict(self) -> dict[str, object]:
        return {
            "machine": self.machine,
            "agent_id": self.agent_id,
            "session_id": self.session_id,
            "name": self.name,
            "shell_pid": self.shell_pid,
            "shell_birth": self.shell_birth,
            "operation": self.operation,
            "acquired_at": self.acquired_at,
            "reason": self.reason,
            "closed_at": self.closed_at,
        }


def journal_dir() -> Path:
    """The durable record directory; creation is deferred to the first write."""
    return ava_home() / "state" / "pty-close-notices"


def record_close(
    *,
    machine: str,
    name: str,
    shell_pid: int,
    shell_birth: str,
    operation: str,
    acquired_at: datetime,
) -> Path | None:
    """Durably record one verified-closed busy session; None when not an agent shell.

    The caller guarantees the session was busy and its exact process identity
    verified gone. Returns the record path, or None when the session name is
    not an agent-owned shell (the canonical ``-agent-<id>-shell-<sid>`` shape).
    """
    match = _AGENT_SHELL_RE.search(name)
    if match is None:
        return None
    notice = ClosureNotice(
        machine=machine,
        agent_id=int(match.group(1)),
        session_id=int(match.group(2)),
        name=name,
        shell_pid=shell_pid,
        shell_birth=shell_birth,
        operation=operation,
        acquired_at=acquired_at.astimezone(UTC).isoformat()
        if acquired_at.tzinfo
        else acquired_at.isoformat(),
        reason=_REASON,
        closed_at=datetime.now(UTC).isoformat(),
    )
    _write_atomic(notice)
    return _record_path(notice)


def _record_path(notice: ClosureNotice) -> Path:
    return journal_dir() / f"{notice.agent_id}_{notice.session_id}_{notice.dedup_key()}.json"


def _write_atomic(notice: ClosureNotice) -> None:
    path = _record_path(notice)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(notice.as_dict(), stream, separators=(",", ":"), sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        Path(raw_tmp).replace(path)
        if os.name != "nt":
            fd_dir = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(fd_dir)
            finally:
                os.close(fd_dir)
    except BaseException:
        with suppress(OSError):
            Path(raw_tmp).unlink()
        raise


def _text(raw: dict[str, object], key: str) -> str | None:
    value = raw.get(key)
    return value if isinstance(value, str) else None


def _integer(raw: dict[str, object], key: str) -> int | None:
    value = raw.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _read(path: Path) -> ClosureNotice | None:
    try:
        raw = json.loads(path.read_text())
        if not isinstance(raw, dict):
            return None
        raw = cast("dict[str, object]", raw)
        machine = _text(raw, "machine")
        name = _text(raw, "name")
        shell_birth = _text(raw, "shell_birth")
        operation = _text(raw, "operation")
        acquired_at = _text(raw, "acquired_at")
        reason = _text(raw, "reason")
        closed_at = _text(raw, "closed_at")
        agent_id = _integer(raw, "agent_id")
        session_id = _integer(raw, "session_id")
        shell_pid = _integer(raw, "shell_pid")
        if (
            machine is None
            or name is None
            or shell_birth is None
            or operation is None
            or acquired_at is None
            or reason is None
            or closed_at is None
            or agent_id is None
            or session_id is None
            or shell_pid is None
        ):
            return None
        notice = ClosureNotice(
            machine=machine,
            agent_id=agent_id,
            session_id=session_id,
            name=name,
            shell_pid=shell_pid,
            shell_birth=shell_birth,
            operation=operation,
            acquired_at=acquired_at,
            reason=reason,
            closed_at=closed_at,
        )
    except (ValueError, KeyError, TypeError, OSError):
        return None
    if notice.dedup_key() not in path.name:
        return None
    return notice


def _content(notice: ClosureNotice) -> str:
    return (
        f"Shell session {notice.name!r} (id {notice.session_id}, agent {notice.agent_id}) "
        f"was closed by {notice.reason} on {notice.machine}, interrupting a running task. "
        f"Recreate the session if its work is still needed (operation {notice.operation})."
    )


def _deliver(pool: ConnectionPool, notice: ClosureNotice) -> None:
    """Deliver one notice exactly once; raise to keep the record for a retry.

    The idempotency claim and the inbound insert commit in ONE transaction: a
    crash before the commit rolls both back (the retry re-delivers), a crash
    after it makes the retry skip the insert and only delete the record —
    never a duplicate inbound (issue #2044 acceptance #4).
    """
    with write_transaction(pool) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO api_idempotency (key, method, path, response_body, op_status, completed_at) "
            "VALUES (%s, 'ops', 'closure-notice', %s, 'completed', now()) "
            "ON CONFLICT (key) DO NOTHING RETURNING key",
            (
                f"closure-notice:{notice.machine}:{notice.dedup_key()}",
                json.dumps(notice.as_dict(), default=str),
            ),
        )
        owned = cur.fetchone() is not None
        if not owned:
            # Already delivered by an earlier flush whose record deletion was
            # interrupted — consume the record without a second inbound.
            return
        cur.execute("SELECT status FROM agents_meta WHERE id = %s", (notice.agent_id,))
        row = cur.fetchone()
        status = row[0] if row is not None else None
        if status not in _NOTIFIABLE_STATUSES:
            logger.info(
                "[pty-close-notices] notice for agent %s (status %s) dropped — never resurrect",
                notice.agent_id,
                status,
            )
            return
        inbound_id = insert_inbound_message(
            conn,
            notice.agent_id,
            _content(notice),
            source="system",
            payload={"closure": notice.as_dict()},
            provenance=InboundProvenance(source_verified_by=None, source_transport="ops"),
        )
    publish_inbound_wake(notice.agent_id, str(inbound_id))
    logger.info(
        "[pty-close-notices] delivered closure notice for agent %s session %s",
        notice.agent_id,
        notice.session_id,
    )


def flush(pool: ConnectionPool) -> int:
    """Deliver every recorded closure notice once; return the number of records left.

    A record that failed to deliver stays in place for the next attempt and is
    logged at ERROR so the gap stays visible (issue #2044 acceptance #5). An
    unreadable record also stays — deleting it would erase an undelivered
    closure without a trace.
    """
    journal = journal_dir()
    if not journal.is_dir():
        return 0
    remaining = 0
    for path in sorted(journal.iterdir()):
        if path.suffix != ".json" or not path.is_file():
            continue
        notice = _read(path)
        if notice is None:
            logger.warning("[pty-close-notices] unreadable record kept for inspection: %s", path)
            remaining += 1
            continue
        try:
            _deliver(pool, notice)
        except Exception:
            logger.exception(
                "[pty-close-notices] delivery failed for agent %s session %s; record kept: %s",
                notice.agent_id,
                notice.session_id,
                path,
            )
            remaining += 1
            continue
        with suppress(OSError):
            path.unlink()
    return remaining
