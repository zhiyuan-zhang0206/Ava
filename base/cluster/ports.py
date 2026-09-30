"""Port-block allocation + per-record port reads.

Allocates a free contiguous block from `base.host.env.port_block` at cluster birth
(`allocate_ports` — overlap-aware against every registered record's block,
live-bind-checked), and reads the service ports back off a record
(`record_app_port` / `record_pgbouncer_port` / `record_postgres_port` /
`record_redis_port` / `record_health_port`): every record born today carries
the full block; only the default home may fall back to its fixed legacy
values (`is_default_home`), because its ports are the pre-registry design,
not a block allocation.
"""

from __future__ import annotations

import socket
from pathlib import Path
from typing import NotRequired, TypedDict, cast

from base import cluster
from base.host.env.port_block import (
    BLOCK_MAX,
    BLOCK_SIZE,
    BLOCK_START,
    LEGACY_AVA_PORTS,
    PORT_OFFSETS,
)


class ClusterPorts(TypedDict):
    """The service->port map a cluster's registry record carries — the fixed,
    closed set of `PORT_OFFSETS` service names, typed. A dict at runtime, so the
    host registry's on-disk JSON shape is byte-for-byte unchanged (only the keys a
    record actually holds are serialized).

    Late-added slots are typed NotRequired because registry records born
    before a slot existed lack the key; reads for those go through the
    helpers (`record_pgbouncer_port` / `record_health_port`), never a bare
    `ports[key]`. Records born today carry the full block."""

    gateway: int
    frontend: int
    app: NotRequired[int]
    heartbeat: NotRequired[int]
    restarter: int  # Retired registry slot, retained for existing home records.
    labeler: int
    task_maintenance: NotRequired[int]
    memory_indexer: int
    ops: int
    milvus: int
    browser: int
    permissions_helper: int
    postgres: int
    redis: int
    pgbouncer: NotRequired[int]
    events_maintenance: NotRequired[int]
    delivery_watchdog: NotRequired[int]
    im_bridge: NotRequired[int]
    page_server: NotRequired[int]
    agent_host: NotRequired[int]
    coordinator: NotRequired[int]  # Retired registry slot, retained for existing home records.
    pg_backup: NotRequired[int]
    pitr_uploader: NotRequired[int]
    pitr_base_backup: NotRequired[int]
    memory_search: NotRequired[int]
    gateway_watchdog: NotRequired[int]
    agent_runner_watchdog: NotRequired[int]


def _record_port(rec: cluster.ClusterRecord, key: str) -> int:
    """The record's port for `key`.

    A record missing a block key is corrupt — raise, never guess a neighbour's
    port (records are born with the full block; only the default home has a
    fixed legacy fallback, handled by the callers)."""
    port = cast("int | None", rec.ports.get(key))  # type: ignore[literal-required]
    if port is None:
        raise KeyError(f"cluster record {rec.gateway_home!r} lacks the {key!r} port")
    return port


def port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def allocate_ports() -> ClusterPorts:
    """Scan [BLOCK_START, BLOCK_MAX) for a contiguous block with no port bound on
    this host right now, and return the service->port map for that base.

    A home knows no other cluster, so a stopped cluster's block looks free and can
    be handed out again. The two homes then share ports, and whichever starts
    second fails `ava start`'s port preflight instead of binding (the user's
    ruling: probe at birth, refuse at start)."""
    for base in range(BLOCK_START, BLOCK_MAX, BLOCK_SIZE):
        if all(cluster.port_free(base + off) for off in PORT_OFFSETS.values()):
            # PORT_OFFSETS' keys ARE the ClusterPorts service names; the dynamic
            # comprehension is the runtime source of that closed set.
            return cast("ClusterPorts", {svc: base + off for svc, off in PORT_OFFSETS.items()})
    raise RuntimeError(f"no free port block in [{BLOCK_START},{BLOCK_MAX})")


def record_app_port(rec: cluster.ClusterRecord) -> int:
    """This cluster's Next.js app port (the gate's upstream).

    The default home keeps its fixed legacy value (frontend+1); every other
    record carries the port. A missing key on an allocated record is a corrupt
    record — KeyError, never a guessed neighbor's port."""
    if cluster.is_default_home(Path(rec.gateway_home)):
        return rec.ports.get("app", cast("int", LEGACY_AVA_PORTS.get("app")))
    return _record_port(rec, "app")


def record_memory_search_port(rec: cluster.ClusterRecord) -> int:
    """This cluster's memory search service port (offset 24)."""
    return _record_port(rec, "memory_search")


def record_pgbouncer_port(rec: cluster.ClusterRecord) -> int:
    """This cluster's PgBouncer listener port.

    The default home keeps its fixed legacy 6433; every other record carries
    the port. A REGISTRY fact only (data-plane bring-up + admin plane); since
    F8b it is not materialized into `.env`."""
    if cluster.is_default_home(Path(rec.gateway_home)):
        return rec.ports.get("pgbouncer", cast("int", LEGACY_AVA_PORTS.get("pgbouncer")))
    return _record_port(rec, "pgbouncer")


def record_postgres_port(rec: cluster.ClusterRecord) -> int:
    """This cluster's direct Postgres port.

    The default home keeps its fixed legacy 5433; every other record carries
    the port. The admin plane's dial when AVA_DB_URL names the pooler."""
    if cluster.is_default_home(Path(rec.gateway_home)):
        return rec.ports.get("postgres", cast("int", LEGACY_AVA_PORTS.get("postgres")))
    return _record_port(rec, "postgres")


def record_redis_port(rec: cluster.ClusterRecord) -> int:
    """This cluster's Redis port.

    The default home keeps its fixed legacy 6380; every other record carries
    the port. Healthchecks use this registry fact rather than inferring a port
    from a URL, which may name a reachable host rather than Redis's loopback
    listener."""
    if cluster.is_default_home(Path(rec.gateway_home)):
        return rec.ports.get("redis", cast("int", LEGACY_AVA_PORTS.get("redis")))
    return _record_port(rec, "redis")


def record_health_port(rec: cluster.ClusterRecord, svc: str) -> int:
    """The health port a cluster's OWN install writes into its OWN `.env` for
    daemon `svc` (one of the PORT_OFFSETS keys whose health servers share the
    daemon name).

    This is the install-time producer only. A health port is a per-UNIT fact
    (`base.host.env.registry.health_port_env_aliases()`), so nothing hands this value to another
    unit — a second unit sharing the machine's localhost namespace states its own
    base instead (`ava start --health-port-base`).

    The default home keeps its fixed legacy value; every other record carries
    the port. A missing key on an allocated record is a corrupt record —
    KeyError, never a guessed neighbor's offset."""
    if cluster.is_default_home(Path(rec.gateway_home)):
        return rec.ports.get(svc, cast("int", LEGACY_AVA_PORTS.get(svc)))  # type: ignore[literal-required]
    return _record_port(rec, svc)
