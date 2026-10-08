"""The fixed port table: every service's listening port, one number each.

A host runs one cluster, so there is nothing to allocate and nothing to keep
apart: every new home records this table as its ports at birth, and a unit whose
`.env` names no port binds the same numbers through `daemon.health.DEFAULT_PORTS`
(derived from here). The table sits in a module that imports nothing because the
settings-free half of the boot chain (`base.daemon.health`, the `.env` registry)
needs it and must import neither `base.config` nor `base.cluster`.

The table is closed. A home's recorded `ports` must carry exactly these keys
(`cli.start_identity` refuses a record with more or fewer), so adding or removing
a slot means rewriting every existing record by hand before its next start.
The numbers 8117 to 8120 belonged to removed slots and are not handed to another
service.

Tests never use these numbers: every port a test binds or dials comes from the
kernel or from a private range above 21000, and `base/cluster/tests/test_fixed_ports.py`
keeps the whole table below it.
"""

from __future__ import annotations

# NOT in any offset order, and "the next number after the last line" is a
# booby trap: it once produced 8113 for `agent_host`, which `ops` already held,
# and the whole suite passed because nothing checked this table for duplicates.
# These are the ports a unit whose `.env` predates a key ACTUALLY BINDS, so a
# duplicate is two daemons fighting over one socket on every existing unit, and
# that collision would have taken the ops server's port, which the gateway dials
# for every runner RPC. Pick a number no other entry holds;
# `test_fixed_ports_are_unique` fails the run if you don't.
FIXED_PORTS: dict[str, int] = {
    "gateway": 8000,
    "frontend": 3000,
    "app": 3001,
    "heartbeat": 8107,
    "labeler": 8103,
    "task_maintenance": 8108,
    "memory_indexer": 8105,
    # ops: NOT 8106 — the Windows iphlpsvc service (svchost) permanently holds
    # 8106 on Windows hosts, so a default there makes ops fail to bind there.
    "ops": 8113,
    "browser": 9222,
    "permissions_helper": 9223,
    "postgres": 5433,
    "redis": 6380,
    "pgbouncer": 6433,
    "events_maintenance": 8109,
    "delivery_watchdog": 8110,
    "im_bridge": 8111,
    "page_server": 8112,
    "agent_host": 8114,
    "pg_backup": 8116,
    "ttl_reaper": 8121,
    "schedule_manager": 8122,
    # The insights read service: it answers on a Unix socket, so this is its /healthz port.
    "insights": 8123,
    # The memory search service's TCP port (not a health port — its healthcheck
    # probes the real /search endpoint).
    "memory_search": 19531,
}
