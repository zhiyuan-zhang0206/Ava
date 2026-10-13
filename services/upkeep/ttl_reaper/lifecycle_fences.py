"""Stuck lifecycle-pointer scan for the TTL reaper service.

``agents_meta.lifecycle_command_id`` is the one durable pointer that says which
accepted lifecycle command (restart / terminate) an agent still owes, and a live
pointer makes every resurrect of that agent defer
(``ops.agents.resurrection_retry.ResurrectSettlementDeferredError``). Only the
original host's serialized turn pump settles a force-terminate
(``base.agents.incarnation.hosted_force.original_host_force``); nothing settles
it on a successor's or a sweep's guess. A force that lands as its host crashes, or
whose machine is decommissioned, therefore leaves the pointer set until an
operator settles it. This scan makes that state loud:

- a command sitting at ``done`` while the pointer is still alive — an
  out-of-band torn write (task #3678);
- an applied force-terminate still unobserved ``STUCK_FORCE_AGE`` after it was
  applied — far past any live host's observation of it.

It runs on an hourly cadence claimed in ``maintenance_state``
(``services/upkeep/ttl_reaper/cadence.py``); this module holds the scan itself.
"""

from __future__ import annotations

import logging

from psycopg_pool import ConnectionPool

from base import telemetry

_log = logging.getLogger(__name__)

# A live original host observes an applied force within moments of its turn
# unwinding; one still unobserved this long after it was applied belongs to a
# host that is gone or wedged.
STUCK_FORCE_AGE = "1 hour"

_STUCK_POINTER = (
    "FROM agents_meta m "
    "JOIN inbound_messages i ON i.id = m.lifecycle_command_id AND i.agent_id = m.id "
    "WHERE i.status = 'done' "
    "OR (i.kind = 'terminate' AND i.status = 'claimed' AND i.applied_at IS NOT NULL "
    f"AND i.observed_at IS NULL AND i.applied_at < clock_timestamp() - interval '{STUCK_FORCE_AGE}')"
)


def scan_stuck_lifecycle_pointers_blocking(pool: ConnectionPool) -> int:
    """Count lifecycle pointers that nothing will settle on its own; alert when any is found.

    Both shapes block every resurrect of the named agent until an operator
    settles the command by hand.
    """
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT count(*) {_STUCK_POINTER}")
        row = cur.fetchone()
        count = 0 if row is None else int(row[0])
        if not count:
            return 0
        cur.execute(f"SELECT m.id {_STUCK_POINTER} ORDER BY m.id LIMIT 5")
        samples = [int(r[0]) for r in cur.fetchall()]
    _log.warning(
        "[ttl-reaper] %d lifecycle pointer(s) will not settle on their own "
        "(agent(s) %s) — resurrection of the named agent(s) defers until an operator "
        "settles the command",
        count,
        ", ".join(str(a) for a in samples),
    )
    telemetry.emit(
        "telemetry",
        event_name="lifecycle_pointer_stuck",
        level="warning",
        attributes={"count": count, "samples": ", ".join(str(a) for a in samples)},
    )
    return count
