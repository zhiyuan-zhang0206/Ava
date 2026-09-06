"""Port-block allocation + per-record port derives.

Allocates a free contiguous block from `shared.port_block` at cluster birth
(`allocate_ports` — overlap-aware against every registered record's block,
live-bind-checked), and derives the service ports a record does not carry
(`record_app_port` / `record_pgbouncer_port` / `record_postgres_port` / `record_redis_port` /
`record_health_port`): a saved record's `ports` is never rewritten, so old
records lack late-added slots and every read derives them deterministically
— the default home's fixed legacy values, or the cluster's own block base
plus the service offset, always inside its reserved block.
"""

from __future__ import annotations

import socket
from pathlib import Path
from typing import NotRequired, TypedDict, cast

from shared import cluster
from shared.port_block import (
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

    `pgbouncer`, `heartbeat`, `task_maintenance`, `events_maintenance`,
    `delivery_watchdog`, and `im_bridge` are NotRequired: records saved before
    those slots existed lack the key, so a read goes through the derived
    helpers (`record_pgbouncer_port` / `record_health_port`), never a bare
    `ports[key]`."""

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
    # Added 2026-08 (S4 isolation): records saved before these slots existed
    # lack the key — reads go through record_health_port, whose late-slot
    # fallback yields the legacy value (the unit's .env predates the key too).
    delivery_watchdog: NotRequired[int]
    im_bridge: NotRequired[int]
    # Added 2026-08 (the hosted agent-runner). Every record that exists today
    # lacks it, and unlike the two above its offset falls outside those records'
    # allocated block — see `_LATE_HEALTH_SLOTS`.
    agent_host: NotRequired[int]
    # Added after the prior block growth; legacy records derive its fixed port.
    pg_backup: NotRequired[int]
    pitr_uploader: NotRequired[int]
    pitr_base_backup: NotRequired[int]
    # Added after the prior block growth; old records' units bind the legacy
    # fallback until their next birth, so the record derive must agree.
    gateway_watchdog: NotRequired[int]
    agent_runner_watchdog: NotRequired[int]


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def allocate_ports(existing_bases: set[int]) -> ClusterPorts:
    """Scan [BLOCK_START, BLOCK_MAX) for a free contiguous block not already
    claimed by a registry record and not bound on the host. Return the
    service->port map for that base."""
    for base in range(BLOCK_START, BLOCK_MAX, BLOCK_SIZE):
        # Skip any candidate whose block OVERLAPS an existing record's block —
        # not just an exact base match. Pre-BLOCK_SIZE-19 records occupy
        # 16-port blocks at 18000+16k, and 19-step candidates land inside them
        # for every k (19k mod 16 cycles all residues), so an exact-base check
        # would let a DOWN cluster's block be re-allocated while its record
        # still owns it —
        # a silent collision the moment both start. Overlap is the honest test:
        # candidate [base, base+BLOCK_SIZE-1] vs legacy record [eb, eb+15].
        if any(base - 15 <= eb <= base + (BLOCK_SIZE - 1) for eb in existing_bases):
            continue
        if all(cluster._port_free(base + off) for off in PORT_OFFSETS.values()):
            # PORT_OFFSETS' keys ARE the ClusterPorts service names; the dynamic
            # comprehension is the runtime source of that closed set.
            return cast("ClusterPorts", {svc: base + off for svc, off in PORT_OFFSETS.items()})
    raise RuntimeError(f"no free port block in [{BLOCK_START},{BLOCK_MAX})")


def record_app_port(rec: cluster.ClusterRecord) -> int:
    """This cluster's Next.js app port (the gate's upstream), deriving it for
    records saved before the `app` slot existed.

    Same pattern as `record_pgbouncer_port`: a saved record's `ports` is never
    rewritten, so old records lack the key. The default home uses its fixed
    legacy value (frontend+1); an allocated cluster uses its own block base
    plus the app offset — always inside the cluster's own reserved block,
    so nothing else on the host holds it.
    """
    port = rec.ports.get("app")
    if port is not None:
        return port
    if cluster.is_default_home(Path(rec.gateway_home)):
        # The full legacy literal always carries app; `.get` only because the
        # key is NotRequired on the shared ClusterPorts type (older records lack it).
        return cast("int", LEGACY_AVA_PORTS.get("app"))
    return rec.ports["gateway"] + PORT_OFFSETS["app"]


def record_memory_search_port(rec: cluster.ClusterRecord) -> int:
    """This cluster's memory search service port (offset 24), deriving it for
    records saved before the slot existed.

    Records born before offset 24 own smaller blocks (16..24 ports), so
    `base + 24` would land inside the NEXT cluster's block — the same reason
    agent_host derives its legacy value for old records. Records lacking the
    key therefore always fall back to the legacy 19531 (the port the unit's
    .env predating the key also binds); fresh births carry the key and read it
    back."""
    port = rec.ports.get("memory_search")
    if port is not None:
        return port
    return LEGACY_AVA_PORTS["memory_search"]


def record_pgbouncer_port(rec: cluster.ClusterRecord) -> int:
    """This cluster's PgBouncer listener port, deriving it for records saved before
    the `pgbouncer` slot existed (the prod default home + any pre-existing cluster).

    A saved record's `ports` is never rewritten, so `rec.ports` may lack the key.
    Derive it deterministically: the default home uses its fixed legacy 6433; an
    allocated cluster its block base plus the pgbouncer offset — always inside
    the cluster's own port block. A REGISTRY fact only (data-plane bring-up +
    admin plane); since F8b it is not materialized into `.env`."""
    port = rec.ports.get("pgbouncer")
    if port is not None:
        return port
    if cluster.is_default_home(Path(rec.gateway_home)):
        # The full legacy literal always carries pgbouncer; `.get` only because the
        # key is NotRequired on the shared ClusterPorts type (older records lack it).
        return cast("int", LEGACY_AVA_PORTS.get("pgbouncer"))
    return rec.ports["gateway"] + PORT_OFFSETS["pgbouncer"]


def record_postgres_port(rec: cluster.ClusterRecord) -> int:
    """This cluster's direct Postgres port (defensive derive; every real record
    carries it). Same pattern as `record_pgbouncer_port`: default home = legacy
    5433, allocated cluster = base + offset. The admin plane's dial when
    AVA_DB_URL names the pooler."""
    # `.get` on the REQUIRED key types as int; widen for the defensive read.
    port = cast("int | None", rec.ports.get("postgres"))
    if port is not None:
        return port
    if cluster.is_default_home(Path(rec.gateway_home)):
        return cast("int", LEGACY_AVA_PORTS.get("postgres"))
    return rec.ports["gateway"] + PORT_OFFSETS["postgres"]


def record_redis_port(rec: cluster.ClusterRecord) -> int:
    """This cluster's Redis port (defensive derive; every real record carries it).

    Same pattern as ``record_postgres_port``: the default home gets its legacy
    port, while an allocated cluster derives the Redis offset inside its own
    block. Healthchecks use this registry fact rather than inferring a port from
    a URL, which may name a reachable host rather than Redis's loopback listener.
    """
    port = cast("int | None", rec.ports.get("redis"))
    if port is not None:
        return port
    if cluster.is_default_home(Path(rec.gateway_home)):
        return cast("int", LEGACY_AVA_PORTS.get("redis"))
    return rec.ports["gateway"] + PORT_OFFSETS["redis"]


# Health-port slots added after every currently-existing registry record was
# born (the S4 isolation pass, 2026-08). A record lacking such a key belongs
# to a unit whose `.env` also lacks it, so the daemon binds the legacy
# fallback at runtime — the derive must agree with that, or the port-preflight
# map would name a port nothing binds.
#
# `agent_host` and `pg_backup` are here for a second, sharper reason: their
# offsets land outside blocks records allocated before those slots actually own.
# Deriving the legacy value keeps every answer inside the ports the record was
# born with, so growing the block can never rename a running neighbour's port.
_LATE_HEALTH_SLOTS = frozenset(
    {
        "im_bridge",
        "delivery_watchdog",
        "agent_host",
        "pg_backup",
        "pitr_uploader",
        "pitr_base_backup",
        "gateway_watchdog",
        "agent_runner_watchdog",
    }
)


def record_health_port(rec: cluster.ClusterRecord, svc: str) -> int:
    """Derive the health port a cluster's OWN install writes into its OWN `.env`
    for daemon `svc` (one of the PORT_OFFSETS keys whose health servers share the
    daemon name).

    This is the install-time producer only. A health port is a per-UNIT fact
    (`shared.env_registry.health_port_env_aliases()`), so nothing hands this value to another
    unit — a second unit sharing the machine's localhost namespace states its own
    base instead (`ava enroll --health-port-base`).

    Records saved before the slot existed may lack the key, same pattern as
    `record_pgbouncer_port`: derive it deterministically — the default home
    uses its fixed legacy value; an allocated cluster uses its block base
    plus the service's offset, always inside the cluster's port block.

    One exception: slots that did not exist at ANY existing record's birth —
    the S4 isolation pass (`im_bridge` / `delivery_watchdog`), hosted
    agent-runner (`agent_host`), pg-backup scheduler (`pg_backup`), and PITR
    uploader (`pitr_uploader`), and capability watchdogs. Their
    missing-key derive is the legacy value, never a block offset — see
    `_LATE_HEALTH_SLOTS`."""
    port = rec.ports.get(svc)  # type: ignore[literal-required]
    if port is not None:
        return port
    if cluster.is_default_home(Path(rec.gateway_home)):
        return cast("int", LEGACY_AVA_PORTS.get(svc))
    if svc in _LATE_HEALTH_SLOTS:
        # A slot added AFTER this record's birth: the unit's `.env` predates the
        # key, so the daemon resolves the legacy fallback at runtime
        # (`daemon_health.DEFAULT_PORTS`), never a block offset. Derive the same
        # number here or preflight would expect a port nothing binds. Records
        # that carry the key (post-slot births) returned above.
        return cast("int", LEGACY_AVA_PORTS.get(svc))
    return rec.ports["gateway"] + PORT_OFFSETS[svc]
