"""The ports a home records at birth, and the bind probe.

A new home records the fixed port table (`base.host.env.port_table`) as its
`ports` (`new_home_ports`); every later read is `rec.ports[key]` off the home's
own start intent (`base.cluster.record`). Nothing allocates, and nothing on the
host lists what another home owns.
"""

from __future__ import annotations

import socket
from typing import TypedDict, cast

from base.host.env.port_table import FIXED_PORTS


class ClusterPorts(TypedDict):
    """The service->port map a home's record carries — the closed set of
    `FIXED_PORTS` service names, typed. A dict at runtime, so the start intent's
    on-disk JSON shape is the plain service->port object.

    `cli.start_identity` refuses a record whose keys differ from `FIXED_PORTS`,
    so every key is always present."""

    gateway: int
    frontend: int
    app: int
    heartbeat: int
    labeler: int
    task_maintenance: int
    memory_indexer: int
    ops: int
    browser: int
    permissions_helper: int
    postgres: int
    redis: int
    pgbouncer: int
    events_maintenance: int
    delivery_watchdog: int
    im_bridge: int
    page_server: int
    agent_host: int
    pg_backup: int
    ttl_reaper: int
    schedule_manager: int
    memory_search: int


def port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def new_home_ports() -> ClusterPorts:
    """The ports a newly born home records: the fixed table.

    The one place birth takes its ports from, so the test session can hand a
    test home ports of its own instead (`tests/fixtures/guards.py`)."""
    # FIXED_PORTS' keys ARE the ClusterPorts service names (locked by a test).
    return cast("ClusterPorts", dict(FIXED_PORTS))
