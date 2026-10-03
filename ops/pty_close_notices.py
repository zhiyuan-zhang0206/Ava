"""Close notices for busy persistent shells a unit closes at `ava stop` or loses to a crash (issue #2044).

`ava stop` closes busy persistent-shell sessions in its `terminals` phase,
after the services are down and before the data plane stops. Each session it
VERIFIED closed (its shell's exact identity gone — also when another session
leaves the stop incomplete) gets one system inbound message for its owner
agent, naming why it closed and any process of the session that outlived its
SIGKILL. The stop writes them itself over one short connection
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
notice that could not be written (the database became unreachable) is returned
to the caller, which reports it on stderr; nothing retries it later, because
the closed session's record is gone by any retry.

Delivery is idempotent per (machine, agent_id, session_id, shell-birth): the
`api_idempotency` claim row and the inbound insert commit in one transaction,
so a notice written twice is delivered once.

A pty-sessions service that died uncleanly (a crash, a SIGKILL, a reboot) closes
its sessions no one, so the next service start sweeps its ledger
(`services.pty_sessions.ledger.sweep`) and hands the busy sessions it closed to
a one-shot child, ``python -m ops.pty_close_notices``, which writes their notices
under `CRASH_REASON` with this module's `main`. The service itself stays
database-free: the child is profile-less, so it dials as an operator process, and
a database it cannot reach is logged and nothing else — the service starts and
serves either way.
"""

from __future__ import annotations

import hashlib
import json
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

import psycopg

from base.agents.messages.inbound_provenance import InboundProvenance
from base.cluster.machine import machine_name
from base.db import Database, insert_inbound_message
from base.events.live.bus import EventBus
from base.log import logger
from base.log.sinks import add_sink
from base.native_process.ownership import OwnedProcess, shown_name
from base.sessions.pty import closure
from ops.cluster_status import AGENT_SHELL_RE

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
    processes that outlived the SIGKILL once its shell is verified gone.
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


def _content(notice: ClosureNotice) -> str:
    text = (
        f"Shell session {notice.name!r} (id {notice.session_id}, agent {notice.agent_id}) "
        f"was closed by {notice.reason} on {notice.machine}, "
        # A stop saw the job; a sweep only knows the ledger last did, up to a snapshot ago.
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
            f" Processes of the session the closure could not end are still running: {left}. "
            "Such a process usually belongs to another user (a root sudo), which you may "
            "not signal either."
        )
    return text


def _deliver(db: Database, bus: EventBus, conn: psycopg.Connection, notice: ClosureNotice) -> None:
    """Deliver one notice at most once in one transaction; raise to report it unwritten.

    The idempotency claim and the inbound insert commit together: a failure
    rolls both back, a notice written twice finds the claim and skips the
    insert — never a duplicate inbound (issue #2044 acceptance #4). The
    transaction is declared writable first: a pooled session can default to
    read-only (`base.db.transaction.write_transaction`).
    """
    try:
        with conn.cursor() as cur:
            cur.execute("SET TRANSACTION READ WRITE")
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
            cur.execute("SELECT status FROM agents_meta WHERE id = %s", (notice.agent_id,))
            row = cur.fetchone()
        status = row[0] if row is not None else None
        if owned and status in _NOTIFIABLE_STATUSES:
            insert_inbound_message(
                conn,
                notice.agent_id,
                _content(notice),
                source="system",
                payload={"closure": notice.as_dict()},
                provenance=InboundProvenance(source_verified_by=None, source_transport="ops"),
                database=db,
                bus=bus,
            )
        elif owned:
            logger.info(
                "[pty-close-notices] notice for agent {} (status {}) dropped — never resurrect",
                notice.agent_id,
                status,
            )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    if owned and status in _NOTIFIABLE_STATUSES:
        logger.info(
            "[pty-close-notices] wrote closure notice for agent {} session {}",
            notice.agent_id,
            notice.session_id,
        )


def write_notices(
    db: Database, bus: EventBus, notices: Sequence[ClosureNotice], *, direct: bool
) -> list[tuple[ClosureNotice, Exception]]:
    """Write every notice over one short connection; return those that failed.

    `direct` dials Postgres itself, bypassing the local pooler (a gateway
    unit, whose pooler stops right after the stop's terminal phase); otherwise
    the configured URL is dialed (a runner-only unit: the gateway's database).
    No notice, no connection. The connection is closed before this returns, and
    each notice commits in its own transaction, so one failure does not take
    the others with it. When the connection cannot be made every notice is
    returned with that error.
    """
    if not notices:
        return []
    try:
        conn = db.connect(direct=direct)
    except Exception as exc:
        return [(notice, exc) for notice in notices]
    failed: list[tuple[ClosureNotice, Exception]] = []
    with conn:
        for notice in notices:
            try:
                _deliver(db, bus, conn, notice)
            except Exception as exc:
                failed.append((notice, exc))
    return failed


def main() -> int:
    """Write the crash notices for the busy sessions a ledger sweep closed.

    The one-shot child of the pty-sessions service: reads a `closure.Outcome`
    wire object from stdin, writes a notice per closed busy session under
    `CRASH_REASON` over one pooled connection, and exits. Every failure — an
    unreadable list, no database settings, an unreachable database — is logged
    and returned as exit 1; nothing retries, because the sweep already cleared
    the ledger record of these sessions.
    """
    try:
        outcome = closure.Outcome.from_wire(json.load(sys.stdin))
        notices = notices_for(outcome.closed, reason=CRASH_REASON)
        failed = write_notices(
            Database.from_settings(), EventBus.from_settings(), notices, direct=False
        )
    except Exception as exc:
        logger.error(
            "[pty-close-notices] crash notices not written: {}: {}", type(exc).__name__, exc
        )
        return 1
    for unwritten, exc in failed:
        logger.error(
            "[pty-close-notices] closure notice for session {!r} (agent {}) could not be written: {}: {}",
            unwritten.name,
            unwritten.agent_id,
            type(exc).__name__,
            exc,
        )
    return 1 if failed else 0


if __name__ == "__main__":
    add_sink(
        sys.stderr, format="{time:HH:mm:ss.SSS} {level: <5} {message}", level="INFO", colorize=False
    )
    raise SystemExit(main())
