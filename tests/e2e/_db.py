"""Shared e2e DB pollers — wait for an agent row to reach a target lifecycle state.

One generous default timeout in one place. A genuine hang still fails, just at
a higher ceiling. Previously each lifecycle test carried its own copy-pasted
`_wait_for_status` and the defaults had drifted (20s / 20s / 30s); this is the
single source of truth.

Why 90s: generous headroom, not a tight bound. CI runs e2e serially on a
dedicated box (one full stack at a time, `-n 1`), so a turn normally completes
in single-digit seconds; the ceiling only has to clear a transient slow
boot/turn under incidental host load without racing a tight deadline. A real
hang (agent dead, status never flips) still fails, just at a higher ceiling —
and the agent-side stall probe (AVA_NODE_STALL_DUMP_SECONDS) names the blocked frame
in the `agent-*.log` artifact either way.
"""

from __future__ import annotations

import time

import psycopg

from shared.config import settings


def wait_for_status(agent_id: int, target: str, timeout: float = 90.0) -> None:
    """Poll agents_meta.status until it equals target, else raise after timeout."""
    deadline = time.monotonic() + timeout
    last: str | None = None
    while time.monotonic() < deadline:
        with psycopg.connect(settings.data_plane.db_url) as conn, conn.cursor() as cur:
            cur.execute("SELECT status FROM agents_meta WHERE id = %s", (agent_id,))
            row = cur.fetchone()
        last = row[0] if row else None
        if last == target:
            return
        time.sleep(0.3)
    raise RuntimeError(
        f"agent {agent_id} {timeout}s did not reach status={target!r} (last={last!r})"
    )
