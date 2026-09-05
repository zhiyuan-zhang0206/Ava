import pytest

from shared import cluster
from shared.port_block import PORT_OFFSETS


def test_legacy_ava_ports():
    assert cluster.LEGACY_AVA_PORTS["gateway"] == 8000
    assert cluster.LEGACY_AVA_PORTS["milvus"] == 19530
    # `main` owns its data plane too, so LEGACY_AVA_PORTS carries every per-cluster
    # service port including pg/redis (its own fixed instance ports).
    assert set(cluster.LEGACY_AVA_PORTS) == set(PORT_OFFSETS)


def test_legacy_ports_are_unique():
    """No two services may share a legacy port.

    These are the ports a unit whose `.env` predates a key ACTUALLY BINDS
    (`daemon_health.DEFAULT_PORTS` is derived from this table), so a duplicate
    is not a cosmetic clash — the two daemons fight over one socket on every
    existing unit. The table is deliberately not in offset order (`ops` moved
    off 8106 to dodge the Windows iphlpsvc grab), so "next number after the last
    line" is not a safe way to add one: agent_host was written as 8113, which
    `ops` already held, and nothing caught it until a boot log printed the same
    port twice.
    """
    ports = cluster.LEGACY_AVA_PORTS
    collisions = {
        port: sorted(svc for svc, p in ports.items() if p == port)
        for port in {p for p in ports.values() if list(ports.values()).count(p) > 1}
    }
    assert not collisions, f"legacy ports shared by more than one service: {collisions}"


def test_per_cluster_pg_redis_ports():
    """Every cluster carries its own Postgres+Redis port, so two co-located clusters
    never share a data plane. `main` gets fixed 5433/6380 (its own instance, off the
    default 5432/6379); allocated dev clusters get pg/redis inside their block."""
    assert "postgres" in PORT_OFFSETS
    assert "redis" in PORT_OFFSETS
    assert cluster.LEGACY_AVA_PORTS["postgres"] == 5433
    assert cluster.LEGACY_AVA_PORTS["redis"] == 6380


def test_allocated_block_includes_pg_redis(monkeypatch: pytest.MonkeyPatch):
    """A freshly allocated (non-main) block gives pg/redis their own ports inside
    the block, distinct from every other service and from each other."""
    monkeypatch.setattr(cluster, "_port_free", lambda _: True)  # pyright: ignore[reportUnknownArgumentType]
    ports = cluster.allocate_ports(existing_bases=set())
    assert ports["postgres"] == 18011
    assert ports["redis"] == 18012
    assert len(set(ports.values())) == len(ports)


def test_allocate_ports_first_block(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(cluster, "_port_free", lambda _: True)  # pyright: ignore[reportUnknownArgumentType]
    ports = cluster.allocate_ports(existing_bases=set())
    assert ports["gateway"] == 18000
    assert ports["milvus"] == 18008
    assert ports.get("memory_search") == 18024


def test_allocate_ports_skips_used_base(monkeypatch: pytest.MonkeyPatch):
    """An existing record's exact base is skipped; with BLOCK_SIZE=27 the next
    candidate is 18027 (the two capability watchdog health listeners extended
    the block after the R3 page_server, hosted-runner, and backup additions;
    offset 20 remains deliberately vacant; overlap-aware skipping lives in
    test_cluster_env).

    Concrete on purpose, like its sibling in test_cluster_env: a block growth
    must force someone to re-check allocation rather than slide past a
    derived assertion."""
    monkeypatch.setattr(cluster, "_port_free", lambda _: True)  # pyright: ignore[reportUnknownArgumentType]
    ports = cluster.allocate_ports(existing_bases={18000})
    assert ports["gateway"] == 18027
