"""Durable close-notice outbox for busy persistent shells a unit closes (issue #2044).

`ava stop` closes busy persistent-shell sessions AFTER the gateway and ops
server are already down, so the closure notice for each owner agent cannot be
delivered synchronously. It records one notice per busy session under
``$AVA_HOME/state/pty-close-notices/`` — durable across the data-plane
shutdown — naming why it closed. It records only sessions it VERIFIED closed
(its shell's exact identity gone — also when another session leaves the stop
incomplete): its closure may be refused. The notice names any process of the
session that outlived its SIGKILL.

The ops daemon flushes the journal at its next startup, once that start has
released its maintenance hold (`services/agent_ops/close_notices.py`): a
notice for a live owner becomes a system inbound message, one for a
terminated owner is dropped without delivery (a closure notice
must never resurrect a dead agent — the TTL reaper's boundary,
gateway/ttl_reaper/__init__.py:83).

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
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from psycopg_pool import ConnectionPool

from base.agents.messages.inbound_provenance import InboundProvenance
from base.db import insert_inbound_message, publish_inbound_wake
from base.db.transaction import write_transaction
from base.host.atomic_io import write_text_atomic
from base.log import logger
from base.native_process.ownership import shown_name
from base.paths import ava_home
from ops.cluster_status import AGENT_SHELL_RE

# The reaper's notifiable boundary: only these statuses receive a closure
# notice; anything else (terminated / missing) drops the record.
_NOTIFIABLE_STATUSES = ("running", "idling")

# Why a unit closed the session, as the owner's notice names it. A pause
# retains terminals and records nothing.
STOP_REASON = "an operator stop (ava stop)"


@dataclass(frozen=True)
class ClosureNotice:
    """One closed busy session and the stop that closed it.

    `survivors` are the session's processes that outlived the closure's SIGKILL
    (typically another user's, which neither the closure nor the agent may
    signal), as (pid, command name); empty when every process is gone. They are
    not part of the dedup key: the notice is about the shell.
    """

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
    survivors: tuple[tuple[int, str], ...] = ()

    def dedup_key(self) -> str:
        raw = f"{self.machine}|{self.agent_id}|{self.session_id}|{self.shell_birth}"
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    def as_dict(self) -> dict[str, object]:
        record: dict[str, object] = {
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
        if self.survivors:
            record["survivors"] = [{"pid": pid, "name": name} for pid, name in self.survivors]
        return record


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
    reason: str,
    survivors: Sequence[tuple[int, str]] = (),
) -> Path | None:
    """Durably record one closed busy session; None when not an agent shell.

    The caller guarantees the session was busy and that it closes it for
    `reason`; `survivors` names, as (pid, command name), the session's
    processes that outlived the SIGKILL once its shell is verified gone.
    Returns the record path, or None when the session name is not an
    agent-owned shell (the canonical ``-agent-<id>-shell-<sid>`` shape).
    """
    match = AGENT_SHELL_RE.search(name)
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
        reason=reason,
        closed_at=datetime.now(UTC).isoformat(),
        survivors=tuple(survivors),
    )
    _write_atomic(notice)
    return _record_path(notice)


def _record_path(notice: ClosureNotice) -> Path:
    return journal_dir() / f"{notice.agent_id}_{notice.session_id}_{notice.dedup_key()}.json"


def _write_atomic(notice: ClosureNotice) -> None:
    path = _record_path(notice)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_text_atomic(
        path,
        json.dumps(notice.as_dict(), separators=(",", ":"), sort_keys=True),
        sync_parent=os.name != "nt",
        suppress_cleanup_error=True,
    )


def _text(raw: dict[str, object], key: str) -> str | None:
    value = raw.get(key)
    return value if isinstance(value, str) else None


def _integer(raw: dict[str, object], key: str) -> int | None:
    value = raw.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _survivors(raw: dict[str, object]) -> tuple[tuple[int, str], ...] | None:
    """The record's survivor list; None when it is malformed (an absent one is empty)."""
    entries = raw.get("survivors", [])
    if not isinstance(entries, list):
        return None
    survivors: list[tuple[int, str]] = []
    for entry in cast("list[object]", entries):
        if not isinstance(entry, dict):
            return None
        fields = cast("dict[str, object]", entry)
        pid, name = _integer(fields, "pid"), _text(fields, "name")
        if pid is None or name is None:
            return None
        survivors.append((pid, name))
    return tuple(survivors)


_TEXT_FIELDS = ("machine", "name", "shell_birth", "operation", "acquired_at", "reason", "closed_at")
_INTEGER_FIELDS = ("agent_id", "session_id", "shell_pid")


def _notice(raw: dict[str, object]) -> ClosureNotice | None:
    """The notice a parsed record describes; None when any field is missing or malformed."""
    texts = {key: value for key in _TEXT_FIELDS if (value := _text(raw, key)) is not None}
    integers = {key: value for key in _INTEGER_FIELDS if (value := _integer(raw, key)) is not None}
    survivors = _survivors(raw)
    if len(texts) < len(_TEXT_FIELDS) or len(integers) < len(_INTEGER_FIELDS):
        return None
    if survivors is None:
        return None
    return ClosureNotice(
        machine=texts["machine"],
        agent_id=integers["agent_id"],
        session_id=integers["session_id"],
        name=texts["name"],
        shell_pid=integers["shell_pid"],
        shell_birth=texts["shell_birth"],
        operation=texts["operation"],
        acquired_at=texts["acquired_at"],
        reason=texts["reason"],
        closed_at=texts["closed_at"],
        survivors=survivors,
    )


def _read(path: Path) -> ClosureNotice | None:
    try:
        raw = json.loads(path.read_text())
        if not isinstance(raw, dict):
            return None
        notice = _notice(cast("dict[str, object]", raw))
    except (ValueError, KeyError, TypeError, OSError):
        return None
    if notice is None or notice.dedup_key() not in path.name:
        return None
    return notice


def _content(notice: ClosureNotice) -> str:
    text = (
        f"Shell session {notice.name!r} (id {notice.session_id}, agent {notice.agent_id}) "
        f"was closed by {notice.reason} on {notice.machine}, interrupting a running task. "
        f"Recreate the session if its work is still needed (operation {notice.operation})."
    )
    if notice.survivors:
        left = ", ".join(f"pid {pid} ({shown_name(name)})" for pid, name in notice.survivors)
        text += (
            f" Processes of the session the closure could not end are still running: {left}. "
            "Such a process usually belongs to another user (a root sudo), which you may "
            "not signal either."
        )
    return text


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
                "[pty-close-notices] notice for agent {} (status {}) dropped — never resurrect",
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
        "[pty-close-notices] delivered closure notice for agent {} session {}",
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
            logger.warning("[pty-close-notices] unreadable record kept for inspection: {}", path)
            remaining += 1
            continue
        try:
            _deliver(pool, notice)
        except Exception:
            logger.exception(
                "[pty-close-notices] delivery failed for agent {} session {}; record kept: {}",
                notice.agent_id,
                notice.session_id,
                path,
            )
            remaining += 1
            continue
        with suppress(OSError):
            path.unlink()
    return remaining
