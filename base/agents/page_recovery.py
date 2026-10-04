"""What the two page-recovery passes share: the notice an agent gets when its
`show()` page died, and the statements that close the row.

A `show()` page (no `serve_dir`) ran its server inside the agent's own process, so a
dead server cannot be rebuilt: the row is closed and the agent is told once to
re-show it. Two passes do this, one async inside the agent (boot and heartbeat,
`agent/startup/_page_reconcile.py`) and one synchronous in the page-server service
(`services/agent_runner/page_server/dead_pages.py`); they differ only in the connection they
use, so the wording, the dedupe window and the SQL live here.
"""

from __future__ import annotations

# The re-serve notice prefix — also the dedupe key for the min-interval check.
NOTICE_PREFIX = "Page recovery:"
# Repeated passes (heartbeats every 5 min) while a dead row survives must not nag
# the agent; the interval is checked against the agent's own inbound history.
MIN_INTERVAL_S = 6 * 3600

# CAS open -> closed (the same UPDATE close_page uses).
CLOSE_PAGE_SQL = (
    "UPDATE agent_pages SET closed_at = now() "
    "WHERE agent_id = %s AND name = %s AND closed_at IS NULL AND expired_at IS NULL"
)
RECENT_NOTICE_SQL = (
    "SELECT 1 FROM inbound_messages "
    "WHERE agent_id = %s AND source = 'system' "
    "AND content LIKE %s AND created_at > %s LIMIT 1"
)
NOTICE_INSERT_SQL = (
    "INSERT INTO inbound_messages (agent_id, content, kind, source) "
    "VALUES (%s, %s, 'chat', 'system')"
)


def recovery_notice(agent_id: int, names: list[str]) -> str:
    """The re-serve notice content; the prefix doubles as the dedupe key."""
    return (
        f"{NOTICE_PREFIX} page(s) "
        f"{', '.join(repr(n) for n in names)} of agent {agent_id} are no longer "
        "being served (their page server died). Re-serve them with "
        "ava.ui.show() to republish."
    )
