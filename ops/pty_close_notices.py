"""Close notices for busy persistent shells a unit closes at `ava stop` or loses to a crash (issue #2044).

`ava stop` closes busy persistent-shell sessions in its `terminals` phase,
after the services are down and before the data plane stops. Each session it
VERIFIED closed (its shell's exact identity gone — also when another session
leaves the stop incomplete) gets one system inbound message for its owner
agent, naming why it closed and known processes observed still running. The stop writes them itself over one short connection
(`write_notices`) opened and closed inside that phase: the database is still
up, and no pool or background task holds a client connection into the data
plane's shutdown. A notice for a terminated owner is dropped without delivery
(a closure notice must never resurrect a dead agent — the TTL reaper's
boundary, gateway/ttl_reaper/__init__.py:83). The inbound row is pending for
the owner's next claim (the canonical insert's best-effort wake finds no
listener: the owner's agent host is already down).

A gateway unit dials Postgres directly (its own pooler is about to stop); a
runner-only unit has no local data plane and dials its configured URL — the
gateway's database, the same dial the stop's posture write already made. A
stop's notice that could not be written (the database became unreachable) is
returned to the caller, which reports it on stderr; the stop neither retries
nor stages.

`write_notices` writes its whole batch in ONE transaction over that one
connection — every idempotency claim and every inbound commits together or not
at all — so a long sweep cannot leave a tail half-written. Delivery is
idempotent per (machine, agent_id, session_id, shell-birth): the
`api_idempotency` claim row and the inbound insert commit in one transaction,
so a notice written twice is delivered once.

A pty-sessions service that died uncleanly (a crash, a SIGKILL, a reboot)
closes its sessions to no one, so the next service start sweeps its ledger
(`services.agent_runner.pty_sessions.ledger.sweep`) and hands the busy sessions it closed to
a one-shot child, ``python -m ops.pty_close_notices``, which writes their
notices under `CRASH_REASON` with this module's `main`. The service itself
stays database-free; the child is profile-less, so it dials as an operator
process. A child that never gets to write loses nothing: the service stages
the batch on disk first (`close_notices_path()`), the child removes the file
only after every notice of it is written, and whatever stays staged is
re-sent by the next service start — on the same idempotency keys, so a notice
that already committed is skipped, never delivered twice.
"""

from __future__ import annotations

import hashlib
import json
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import psycopg
from psycopg import sql

from base.agents.messages.inbound_provenance import InboundProvenance
from base.cluster.machine import machine_name
from base.db import Database, insert_inbound_message_in_transaction, publish_inbound_wake
from base.events.live.bus import EventBus
from base.host.atomic_io import write_text_atomic
from base.log import logger
from base.log.sinks import add_sink
from base.native_process.ownership import OwnedProcess, shown_name
from base.sessions.pty import closure
from base.sessions.pty.paths import close_notices_path
from ops.cluster_status import AGENT_SHELL_RE
from ops.rpc_schemas import OpStatus

# The reaper's notifiable boundary: only these statuses receive a closure
# notice; anything else (terminated / missing) drops the notice.
_NOTIFIABLE_STATUSES = ("running", "idling")

# Why a unit closed the session, as the owner's notice names it. A pause
# retains terminals and records nothing.
STOP_REASON = "an operator stop (ava stop)"

# The same, for sessions a pty-sessions service that ended uncleanly left behind
# (a crash, a forced stop of the service, a reboot): the sweep found them busy.
CRASH_REASON = "the pty-sessions service ending uncleanly (a crash, a forced stop or a reboot)"


@dataclass(frozen=True)
class ClosureNotice:
    """One closed busy session and the stop that closed it.

    `survivors` are known processes observed alive after best-effort closure,
    as (pid, command name). An empty list does not prove all descendants gone.
    They are
    not part of the dedup key: the notice is about the shell.
    """

    machine: str
    agent_id: int
    session_id: int
    name: str
    shell_pid: int
    shell_birth: str
    reason: str
    closed_at: str
    survivors: tuple[tuple[int, str], ...] = ()
    operation: str | None = None
    acquired_at: str | None = None

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
            "reason": self.reason,
            "closed_at": self.closed_at,
        }
        if self.operation is not None:
            record["operation"] = self.operation
        if self.acquired_at is not None:
            record["acquired_at"] = self.acquired_at
        if self.survivors:
            record["survivors"] = [{"pid": pid, "name": name} for pid, name in self.survivors]
        return record

    @classmethod
    def from_dict(cls, record: Mapping[str, Any]) -> ClosureNotice:
        """Rebuild the notice a staged file recorded, exactly as `as_dict` wrote it.

        A staged batch is the notices an earlier attempt tried to write — same
        key, same text — so the re-send after a failure is a retry of that
        attempt, not a fresh notice about the same shell.
        """
        return cls(
            machine=str(record["machine"]),
            agent_id=int(record["agent_id"]),
            session_id=int(record["session_id"]),
            name=str(record["name"]),
            shell_pid=int(record["shell_pid"]),
            shell_birth=str(record["shell_birth"]),
            reason=str(record["reason"]),
            closed_at=str(record["closed_at"]),
            survivors=tuple(
                (int(item["pid"]), str(item["name"]))
                for item in cast("list[dict[str, Any]]", record.get("survivors") or [])
            ),
            operation=None if record.get("operation") is None else str(record["operation"]),
            acquired_at=None if record.get("acquired_at") is None else str(record["acquired_at"]),
        )


def closure_notice(
    *,
    machine: str,
    name: str,
    shell_pid: int,
    shell_birth: str,
    reason: str,
    operation: str | None = None,
    acquired_at: datetime | None = None,
    survivors: Sequence[tuple[int, str]] = (),
) -> ClosureNotice | None:
    """The notice for one closed busy session; None when not an agent shell.

    The caller guarantees the session was busy and that it closes it for
    `reason`; `survivors` names, as (pid, command name), the session's
    known processes observed alive once its shell is verified gone.
    `operation` and `acquired_at` name the stop's maintenance hold; a sweep
    after a crash has none.
    Returns None when the session name is not an agent-owned shell (the
    canonical ``-agent-<id>-shell-<sid>`` shape).
    """
    match = AGENT_SHELL_RE.search(name)
    if match is None:
        return None
    return ClosureNotice(
        machine=machine,
        agent_id=int(match.group(1)),
        session_id=int(match.group(2)),
        name=name,
        shell_pid=shell_pid,
        shell_birth=shell_birth,
        operation=operation,
        acquired_at=None
        if acquired_at is None
        else acquired_at.astimezone(UTC).isoformat()
        if acquired_at.tzinfo
        else acquired_at.isoformat(),
        reason=reason,
        closed_at=datetime.now(UTC).isoformat(),
        survivors=tuple(survivors),
    )


def shell_birth(shell: OwnedProcess) -> str:
    """The shell's birth as the dedup key spells it: one identity, one spelling, every path."""
    if shell.starttime is not None:
        return f"starttime:{shell.starttime}"
    return f"birth:{shell.birth!r}"


def notices_for(
    closed: Sequence[closure.ClosedSession],
    *,
    reason: str,
    operation: str | None = None,
    acquired_at: datetime | None = None,
) -> list[ClosureNotice]:
    """One notice per closed busy session that is an agent shell, from this machine."""
    machine = machine_name()
    built = (
        closure_notice(
            machine=machine,
            name=session.name,
            shell_pid=session.shell.pid,
            shell_birth=shell_birth(session.shell),
            reason=reason,
            operation=operation,
            acquired_at=acquired_at,
            survivors=session.left,
        )
        for session in closed
    )
    return [notice for notice in built if notice is not None]


# The staged file's shape: a versioned envelope, like the ledger's.
_PENDING_VERSION = 1


def read_pending(path: Path) -> list[ClosureNotice]:
    """The notices staged for delivery; empty when none wait.

    Missing reads empty. An unreadable or malformed file is logged and reads
    empty, like the ledger: never a guessed notice, and the file stays in
    place for the operator (a later stage replaces it with the next batch).
    """
    try:
        raw = json.loads(path.read_text())
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as exc:
        logger.warning(
            "[pty-close-notices] staged notices {path} unreadable ({exc})", path=path, exc=exc
        )
        return []
    try:
        records = cast("list[dict[str, Any]]", raw["notices"])
        return [ClosureNotice.from_dict(record) for record in records]
    except (KeyError, TypeError, ValueError) as exc:
        logger.warning(
            "[pty-close-notices] staged notices {path} malformed ({exc})", path=path, exc=exc
        )
        return []


def write_pending(path: Path, notices: Sequence[ClosureNotice]) -> None:
    """Publish the waiting notices through one atomic replace (owner-only)."""
    payload = {"version": _PENDING_VERSION, "notices": [notice.as_dict() for notice in notices]}
    write_text_atomic(path, json.dumps(payload), mode=0o600, sync_file=False)


def clear_pending(path: Path) -> None:
    """Drop the staged file once every notice in it is written."""
    path.unlink(missing_ok=True)


def _one_per_key(notices: Sequence[ClosureNotice]) -> list[ClosureNotice]:
    """One notice per dedup key, the first occurrence winning.

    Both the staged merge and the database batch need each key spelled once: a
    batch that carried one key twice would claim it once and then deliver every
    copy that finds itself owned — two inbounds for one shell birth.
    """
    unique: list[ClosureNotice] = []
    seen: set[str] = set()
    for notice in notices:
        if notice.dedup_key() not in seen:
            seen.add(notice.dedup_key())
            unique.append(notice)
    return unique


def stage_crash_notices(swept: closure.Outcome, path: Path) -> list[ClosureNotice]:
    """Stage the sweep's closure notices beside any already waiting; return the whole batch.

    The service calls this before its one-shot child runs: everything the child
    must write is on disk first, so a child cut short by the time limit — or
    one a database that answers nothing fails — loses the batch to the next
    start instead. What was already staged stays first (a re-sent notice keeps
    its earlier record); an empty batch leaves the file untouched.
    """
    staged = _one_per_key((*read_pending(path), *notices_for(swept.closed, reason=CRASH_REASON)))
    if staged:
        write_pending(path, staged)
    return staged


def _content(notice: ClosureNotice) -> str:
    text = (
        f"Shell session {notice.name!r} (id {notice.session_id}, agent {notice.agent_id}) "
        f"was closed by {notice.reason} on {notice.machine}, "
        # A stop observed work; a sweep only has the recorded identities.
        + (
            "interrupting a running task. "
            if notice.operation
            else "probably interrupting a running task. "
        )
        + "Recreate the session if its work is still needed"
        + (f" (operation {notice.operation})." if notice.operation else ".")
    )
    if notice.survivors:
        left = ", ".join(f"pid {pid} ({shown_name(name)})" for pid, name in notice.survivors)
        text += (
            f" Known processes observed after terminal closure are still running: {left}. "
            "Inspect their ownership and current work before taking action; terminal "
            "closure does not prove that all background or detached processes ended."
        )
    return text


def _claim_key(notice: ClosureNotice) -> str:
    """The one spelling of a notice's idempotency key, shared by claim and check."""
    return f"closure-notice:{notice.machine}:{notice.dedup_key()}"


def _claim_all(cur: psycopg.Cursor, batch: Sequence[ClosureNotice]) -> set[str]:
    """Claim every notice's idempotency key in one statement; the keys this attempt owns.

    The whole batch goes in as one multi-row `ON CONFLICT (key) DO NOTHING
    RETURNING key`: the returned keys are the notices this attempt may deliver;
    a key an earlier attempt already claimed — delivered then, dropped then, or
    left behind when that attempt died after committing — stays with it, so a
    re-sent notice is skipped, never delivered twice (issue #2044 acceptance
    #4).
    """
    group = sql.SQL("(%s, 'ops', 'closure-notice', %s, %s, now())")
    query = sql.SQL(
        "INSERT INTO api_idempotency (key, method, path, response_body, op_status, completed_at) "
        "VALUES {} ON CONFLICT (key) DO NOTHING RETURNING key"
    ).format(sql.SQL(", ").join([group] * len(batch)))
    params: list[object] = []
    for notice in batch:
        params.append(_claim_key(notice))
        params.append(json.dumps(notice.as_dict(), default=str))
        params.append(OpStatus.COMPLETED)
    cur.execute(query, params)
    return {str(row[0]) for row in cur.fetchall()}


def _owner_statuses(cur: psycopg.Cursor, batch: Sequence[ClosureNotice]) -> dict[int, str | None]:
    """Every owner's status in one statement, for the notifiable check."""
    cur.execute(
        "SELECT id, status FROM agents_meta WHERE id = ANY(%s)",
        (sorted({notice.agent_id for notice in batch}),),
    )
    return {int(row[0]): row[1] for row in cur.fetchall()}


def _write_batch(
    conn: psycopg.Connection, notices: Sequence[ClosureNotice]
) -> list[tuple[ClosureNotice, int]]:
    """Write the batch in the connection's one transaction; return (notice, inbound id) pairs.

    The transaction is declared writable first: a pooled session can default to
    read-only (`base.db.transaction.write_transaction`). Every claim goes in
    one statement, every owner's status is read in one, and each owned notice
    of a notifiable owner gets the canonical inbound insert
    (`insert_inbound_message_in_transaction`; its lineage event is None for a
    system-sourced chat, `base.db._lineage_event`). The single commit is the
    batch's durability point — everything before it rolls back together — and
    a notice this attempt does not own, or one of a terminated or unknown
    owner, is skipped: dropped without delivery and without resurrecting
    anyone.
    """
    delivered: list[tuple[ClosureNotice, int]] = []
    dropped: list[tuple[ClosureNotice, str | None]] = []
    with conn.cursor() as cur:
        cur.execute("SET TRANSACTION READ WRITE")
        owned = _claim_all(cur, notices)
        statuses = _owner_statuses(cur, notices)
        for notice in notices:
            if _claim_key(notice) not in owned:
                continue
            status = statuses.get(notice.agent_id)
            if status in _NOTIFIABLE_STATUSES:
                inbound_id, _event = insert_inbound_message_in_transaction(
                    cur,
                    notice.agent_id,
                    _content(notice),
                    source="system",
                    payload={"closure": notice.as_dict()},
                    provenance=InboundProvenance(source_verified_by=None, source_transport="ops"),
                )
                delivered.append((notice, inbound_id))
            else:
                dropped.append((notice, status))
    conn.commit()
    for notice, status in dropped:
        logger.info(
            "[pty-close-notices] notice for agent {} (status {}) dropped — never resurrect",
            notice.agent_id,
            status,
        )
    for notice, _inbound_id in delivered:
        logger.info(
            "[pty-close-notices] wrote closure notice for agent {} session {}",
            notice.agent_id,
            notice.session_id,
        )
    return delivered


def write_notices(
    db: Database, bus: EventBus, notices: Sequence[ClosureNotice], *, direct: bool
) -> list[tuple[ClosureNotice, Exception]]:
    """Write every notice in one transaction over one short connection; return those that failed.

    `direct` dials Postgres itself, bypassing the local pooler (a gateway
    unit, whose pooler stops right after the stop's terminal phase); otherwise
    the configured URL is dialed (a runner-only unit: the gateway's database).
    No notice, no connection. The batch is one transaction — every claim and
    every inbound commits together or not at all — so a failed batch leaves
    nothing behind (no claim to skip the retry, no inbound that landed alone)
    and returns every notice with the error. The connection is closed before
    this returns. A notice written twice is delivered once: its key is spelled
    once per call, and a call that finds the key claimed skips it.
    """
    batch = _one_per_key(notices)
    if not batch:
        return []
    try:
        conn = db.connect(direct=direct)
    except Exception as exc:
        return [(notice, exc) for notice in batch]
    try:
        with conn:
            delivered = _write_batch(conn, batch)
    except Exception as exc:
        return [(notice, exc) for notice in batch]
    for notice, inbound_id in delivered:
        # The canonical insert's fast path, after the rows are durable; in both
        # flows that reach here the owner's agent host is down, so the wake
        # finds no listener — it costs a best-effort publish either way.
        publish_inbound_wake(db, bus, notice.agent_id, str(inbound_id))
    return []


def main() -> int:
    """Write the closure notices the service staged; the one-shot child of the pty-sessions service.

    Reads `run/pty-close-notices.json` (`stage_crash_notices` puts the sweep's
    notices there before this process starts), writes them all in one
    transaction over one short connection, and removes the file only once
    every notice of it is written. A database it cannot reach leaves the file
    for the next service start to re-send, on the same keys; every failure is
    logged at ERROR with its count and returned as exit 1.
    """
    try:
        path = close_notices_path()
        notices = read_pending(path)
    except Exception as exc:
        logger.error("[pty-close-notices] crash notices not read: {}: {}", type(exc).__name__, exc)
        return 1
    if not notices:
        return 0
    try:
        failed = write_notices(
            Database.from_settings(), EventBus.from_settings(), notices, direct=False
        )
    except Exception as exc:  # no database settings: a failed child, not a service failure
        logger.error(
            "[pty-close-notices] {} closure notice(s) not written: {}: {}",
            len(notices),
            type(exc).__name__,
            exc,
        )
        return 1
    if failed:
        logger.error(
            "[pty-close-notices] {} closure notice(s) not written and stay staged for the "
            "next pty-sessions start to re-send",
            len(notices),
        )
        return 1
    try:
        clear_pending(path)
    except OSError as exc:
        logger.error(
            "[pty-close-notices] {} closure notice(s) written but the staged file could not "
            "be removed ({exc}); the next start re-sends them — each is idempotent",
            len(notices),
            exc=exc,
        )
        return 0
    logger.info("[pty-close-notices] wrote {} closure notice(s)", len(notices))
    return 0


if __name__ == "__main__":
    add_sink(
        sys.stderr, format="{time:HH:mm:ss.SSS} {level: <5} {message}", level="INFO", colorize=False
    )
    raise SystemExit(main())
