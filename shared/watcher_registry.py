"""The watcher registry — the "should it exist?" half of the R1 watcher frame.

`ava.watcher.at/cron/launch` writes one row per spawned watcher session
(`agent_watchers`, Task #1021). The row outlives the session exactly when the
session was KILLED rather than ended: a watcher that exits cleanly deletes its
own row (the generated bootstrap's finally), so a surviving row with a missing
session means "this watcher should exist and does not". The agent's boot
reconcile (`ava.watcher.reconcile`) reads that and rebuilds cron watchers /
marks missed one-shots — the #1014 fix (4th recurrence: rollouts reaped
watcher sessions and nothing knew they should exist).

Liveness is the session itself — a watcher process IS its session — so the
registry deliberately holds no lease: a session gone means the watcher is gone,
and the reconcile is the only reader that needs to know.

Pure DB: no SDK imports, so the watcher child's bootstrap finally can call
`delete_watcher` without pulling the SDK into a short-lived child.
"""

from __future__ import annotations

import logging
from typing import Any

from psycopg.rows import dict_row

from shared.db import connect

logger = logging.getLogger(__name__)

# The statuses the reconcile transitions through. `running` is the spawn state;
# `rebuilt` marks a row whose watcher died and was re-spawned (history kept —
# the rebuild's NEW session has its own `running` row); `missed` marks a
# one-shot whose moment passed while its session was gone.
_WATCHER_STATUSES = ("running", "rebuilt", "missed")

# The rebuild payload columns, per kind. The reconcile rebuilds a watcher by
# re-calling the SDK verb with exactly these arguments, so the row must carry
# everything the verb needs.
_KIND_PAYLOAD = {
    "at": ("message", "fires_at"),
    "cron": ("message", "cron_expr", "cron_timezone", "cron_end_at"),
    "launch": (),
}


def register_watcher(
    agent_id: int,
    session_id: int,
    *,
    kind: str,
    name: str,
    message: str | None = None,
    fires_at: Any = None,
    cron_expr: str | None = None,
    cron_timezone: str | None = None,
    cron_end_at: Any = None,
    timeout_secs: float | None = None,
) -> None:
    """Record a watcher session at spawn (`ava.watcher._spawn`).

    `kind` must be one of at/cron/launch and the row carries that kind's
    rebuild payload (see `_KIND_PAYLOAD`). Fail-soft at the call site: a
    registry write must never break the watcher it is only observing.
    """
    if kind not in _KIND_PAYLOAD:
        raise ValueError(f"unknown watcher kind {kind!r}")
    with connect(autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO agent_watchers (
                session_id, agent_id, kind, name, message, fires_at,
                cron_expr, cron_timezone, cron_end_at, timeout_secs
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (agent_id, session_id) DO NOTHING
            """,
            (
                session_id,
                agent_id,
                kind,
                name,
                message,
                fires_at,
                cron_expr,
                cron_timezone,
                cron_end_at,
                timeout_secs,
            ),
        )


def delete_watcher(agent_id: int, session_id: int) -> None:
    """Drop a watcher's registry row.

    Called from the watcher child's clean-exit finally (fired / ended /
    crashed-with-finally) and from `ava.shell.sessions.kill` (a deliberately
    killed watcher must not be rebuilt at the next boot). A missing row is a
    no-op.

    `session_id` is a PER-AGENT counter (agents_meta.session_index), so the
    row is keyed by (agent_id, session_id) — deleting on session_id alone
    could drop another agent's same-numbered row (task #1155).
    """
    with connect(autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            "DELETE FROM agent_watchers WHERE agent_id = %s AND session_id = %s",
            (agent_id, session_id),
        )


def mark_status(agent_id: int, session_id: int, status: str) -> None:
    """Transition a row's status (`running` → `rebuilt` / `missed`)."""
    if status not in _WATCHER_STATUSES:
        raise ValueError(f"unknown watcher status {status!r}")
    with connect(autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE agent_watchers SET status = %s, updated_at = now() "
            "WHERE agent_id = %s AND session_id = %s",
            (status, agent_id, session_id),
        )


def watcher_rows(agent_id: int | None = None) -> list[dict[str, Any]]:
    """Every watcher registry row, newest first; `agent_id` narrows to one agent.

    The boot reconcile reads this to decide rebuild vs missed; the stop reaper
    reads the session ids to tell watcher kills apart from plain shell kills.
    """
    with connect(autocommit=True) as conn, conn.cursor(row_factory=dict_row) as cur:
        if agent_id is None:
            cur.execute("SELECT * FROM agent_watchers ORDER BY created_at DESC, session_id DESC")
        else:
            cur.execute(
                "SELECT * FROM agent_watchers WHERE agent_id = %s "
                "ORDER BY created_at DESC, session_id DESC",
                (agent_id,),
            )
        return list(cur.fetchall())


def watcher_session_ids(agent_id: int | None = None) -> set[int]:
    """The session ids the registry knows as watchers (all agents, or one)."""
    with connect(autocommit=True) as conn, conn.cursor() as cur:
        if agent_id is None:
            cur.execute("SELECT session_id FROM agent_watchers")
        else:
            cur.execute("SELECT session_id FROM agent_watchers WHERE agent_id = %s", (agent_id,))
        return {r[0] for r in cur.fetchall()}


def killed_watcher_annotations(session_names: list[str]) -> list[str]:
    """The stop-output lines naming which of a reaper's killed shell sessions
    the registry knows as watchers — a stopped host's watchers are exactly
    what the next boot reconcile rebuilds from this table (a session the
    registry does not know is a plain shell, gone for good). Empty when no
    session matches. Fail-soft by contract: the stop reaper runs while the DB
    may already be down, and a registry read must never block the stop it only
    annotates — a read failure yields no lines, and the caller prints whatever
    it gets.
    """
    import re

    try:
        rows = watcher_rows()
    except Exception:
        return []
    # session ids are per-agent (agents_meta.session_index), so key by
    # (agent_id, session_id); the session name carries both.
    names = {(r["agent_id"], str(r["session_id"])): r["name"] for r in rows}
    lines: list[str] = []
    for session in session_names:
        m = re.search(r"-agent-(\d+)-shell-(\d+)(?:-|$)", session)
        if m and (int(m.group(1)), m.group(2)) in names:
            lines.append(
                f"  (watcher '{names[(int(m.group(1)), m.group(2))]}' — session {session}; "
                "rebuilt from the registry at the next boot)"
            )
    return lines
