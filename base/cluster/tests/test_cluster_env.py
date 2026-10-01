from pathlib import Path

import pytest

from base import cluster
from base.host.env import registry


def _rec(tmp_path: Path):
    return cluster.ClusterRecord(
        ports={
            "gateway": 18000,
            "frontend": 18001,
            "heartbeat": 18002,
            "labeler": 18004,
            "task_maintenance": 18005,
            "memory_indexer": 18006,
            "ops": 18007,
            "milvus": 18008,
            "browser": 18009,
            "permissions_helper": 18010,
            "postgres": 18011,
            "redis": 18012,
            "pgbouncer": 18013,
            "events_maintenance": 18014,
            "app": 18015,
            # The birth shape: every slot of the fixed table carries a key.
            "delivery_watchdog": 18016,
            "im_bridge": 18017,
            "agent_host": 18019,
            "pg_backup": 18021,
            "memory_search": 18024,
            "gateway_watchdog": 18025,
            "agent_runner_watchdog": 18026,
            "page_server": 18018,
        },
        gateway_home=str(tmp_path / ".ava-t1"),
        created_at="x",
    )


def test_derive_env_ports_and_urls(tmp_path: Path):
    env = cluster.derive_env(
        _rec(tmp_path),
        base_db_url="postgresql://ava:p@localhost:5432/ava",
        base_redis_url="redis://localhost:6379/0",
        cluster_secret="sekret",  # noqa: S106 — test fixture, not a real secret
        redis_admin_password="admin-value",  # noqa: S106 — isolated test credential
        redis_password="runtime-value",  # noqa: S106 — isolated test credential
        pgbouncer_enabled=False,  # pooling off -> AVA_DB_URL stays on the direct pg port
    )
    # the secret is written into the cluster .env so every cluster process has it
    assert env["AVA_CLUSTER_SECRET"] == "sekret"  # noqa: S105 — test fixture, not a real secret
    assert env["AVA_GATEWAY_PORT"] == "18000"
    # a gateway box reaches its own gateway over loopback (self-call); the address
    # remote runners dial is given at their first start, never stored here
    assert env["AVA_GATEWAY_URL"] == "http://localhost:18000"
    assert env["AVA_GATEWAY_HEALTH_URL"] == "http://localhost:18000/api/health"
    assert env["AVA_FRONTEND_HEALTHCHECK_URL"] == "http://localhost:18001"
    assert env["AVA_MILVUS_PORT"] == "18008"
    assert env["AVA_MILVUS_URI"] == "http://127.0.0.1:18008"
    # db_url + redis_url carry the data-plane identity AS DATA: a fresh birth
    # writes the fixed `ava` db/role/ACL identifier. AVA_DB_URL is the
    # credential-free endpoint (the owner is NOLOGIN; processes dial delivered
    # write-generation logins) — whatever the bearer; there is no owner password.
    # The redis URL keeps the base logical DB (0) — every cluster owns its redis,
    # so there is no per-cluster index swap.
    assert env["AVA_DB_URL"] == "postgresql://ava@localhost:5432/ava"
    assert "AVA_DB_ADMIN_PASSWORD" not in env
    assert env["AVA_REDIS_URL"] == "redis://ava:runtime-value@localhost:6379/0"
    # channels are fixed (single per-cluster redis, no neighbour to prefix away from)
    assert env["AVA_EVENTS_CHANNEL"] == "ava:events"


def test_derive_env_empty_secret_keeps_redis_authenticated(tmp_path: Path):
    """A no-secret cluster's DB URL is the same credential-free endpoint (the
    data plane authenticates with delivered generation logins), and Redis always
    authenticates: its URL carries the runtime password and both Redis
    credentials are persisted."""
    env = cluster.derive_env(
        _rec(tmp_path),
        base_db_url="postgresql://ava:p@localhost:5432/ava",
        base_redis_url="redis://localhost:6379/0",
        cluster_secret="",
        redis_admin_password="admin-value",  # noqa: S106 — isolated test credential
        redis_password="runtime-value",  # noqa: S106 — isolated test credential
        pgbouncer_enabled=False,
    )
    assert env["AVA_CLUSTER_SECRET"] == ""
    assert "AVA_DB_ADMIN_PASSWORD" not in env
    assert env["AVA_DB_URL"] == "postgresql://ava@localhost:5432/ava"
    assert env["AVA_REDIS_URL"] == "redis://ava:runtime-value@localhost:6379/0"
    assert env["AVA_REDIS_ADMIN_PASSWORD"] == "admin-value"  # noqa: S105 — test value
    assert env["AVA_REDIS_PASSWORD"] == "runtime-value"  # noqa: S105 — test value
    # the identities stay readable as data
    from urllib.parse import urlsplit

    assert urlsplit(env["AVA_DB_URL"]).username == "ava"
    assert urlsplit(env["AVA_REDIS_URL"]).username == "ava"


@pytest.mark.parametrize("missing", ["redis_admin_password", "redis_password"])
def test_derive_no_secret_env_refuses_missing_redis_credential(
    tmp_path: Path, missing: str
) -> None:
    credentials = {"redis_admin_password": "admin-value", "redis_password": "runtime-value"}
    credentials[missing] = ""
    with pytest.raises(ValueError, match="Redis always authenticates"):
        cluster.derive_env(
            _rec(tmp_path),
            base_db_url="postgresql://ava@localhost:5432/ava",
            base_redis_url="redis://localhost:6379/0",
            cluster_secret="",
            redis_admin_password=credentials["redis_admin_password"],
            redis_password=credentials["redis_password"],
        )


def test_derived_env_keys_in_sync(tmp_path: Path):
    """derived_env_keys() (used to strip leaked prod values from cluster
    subprocesses) must exactly match the keys derive_env actually produces —
    the registry declaration is the derive surface, this test is the verifier
    that producer and declaration cannot drift. AVA_PGBOUNCER_PORT is
    deliberately on NEITHER side — the pooler port is a registry fact, never
    an env key."""
    env = cluster.derive_env(
        _rec(tmp_path),
        base_db_url="postgresql://ava:p@localhost:5432/ava",
        base_redis_url="redis://localhost:6379/0",
        cluster_secret="sekret",  # noqa: S106 — test fixture, not a real secret
        redis_admin_password="admin-value",  # noqa: S106 — isolated test credential
        redis_password="runtime-value",  # noqa: S106 — isolated test credential
    )
    assert set(env) == registry.derived_env_keys()
    assert "AVA_PGBOUNCER_PORT" not in env


def test_derive_env_pgbouncer_enabled_writes_pooler_port(tmp_path: Path):
    """The one-URL design: with pooling on (the default), AVA_DB_URL is born
    carrying the pooler listener port (record-derived), not the direct pg port —
    and there is still no separate AVA_PGBOUNCER_PORT key."""
    env = cluster.derive_env(
        _rec(tmp_path),
        base_db_url="postgresql://ava:p@localhost:5432/ava",
        base_redis_url="redis://localhost:6379/0",
        cluster_secret="sekret",  # noqa: S106 — test fixture, not a real secret
        redis_admin_password="admin-value",  # noqa: S106 — isolated test credential
        redis_password="runtime-value",  # noqa: S106 — isolated test credential
    )
    from urllib.parse import urlsplit

    assert urlsplit(env["AVA_DB_URL"]).port == 18013
    assert "AVA_PGBOUNCER_PORT" not in env


def test_per_cluster_base_urls_point_at_own_instance_ports(tmp_path: Path):
    """Every cluster's base URLs are loopback at its recorded pg/redis ports —
    derive_env then swaps in the db name + `ava_<cluster>` identity + secret.
    The default (no `data_plane_host` on the record) MUST render exactly this
    local form — the A5 de-hardcoding contract: parameterizing the host source
    is not a behavior change."""
    db, redis = cluster.per_cluster_base_urls(_rec(tmp_path))
    assert db == "postgresql://x@127.0.0.1:18011/postgres"
    assert redis == "redis://127.0.0.1:18012/0"


def test_per_cluster_base_urls_use_record_data_plane_host(tmp_path: Path):
    """A record carrying `data_plane_host` renders its URLs at that host — the
    replaceable source the de-hardcoding exists for (external data plane,
    Task #1752). Ports and everything else stay the record's own."""
    from dataclasses import replace

    rec = replace(_rec(tmp_path), data_plane_host="10.0.0.7")
    db, redis = cluster.per_cluster_base_urls(rec)
    assert db == "postgresql://x@10.0.0.7:18011/postgres"
    assert redis == "redis://10.0.0.7:18012/0"


def test_per_cluster_base_urls_blank_host_falls_back_to_loopback(tmp_path: Path):
    """An explicitly blank `data_plane_host` is the same as absent: loopback.
    The record is born with "" (or an old record loads without the field at
    all), so the fallback IS the default-behavior contract."""
    from dataclasses import replace

    rec = replace(_rec(tmp_path), data_plane_host="  ")
    db, redis = cluster.per_cluster_base_urls(rec)
    assert db == "postgresql://x@127.0.0.1:18011/postgres"
    assert redis == "redis://127.0.0.1:18012/0"


def test_get_record_round_trip_preserves_data_plane_host(tmp_path: Path):
    """`data_plane_host` lives on a home's own record, inside its start intent:
    round-tripping through `get_record` returns it, and a record with no
    `data_plane_host` key at all (an old on-disk shape) loads with the loopback
    default — the compat rule every existing cluster depends on."""
    import json
    from dataclasses import asdict, replace

    home = tmp_path / ".ava-t1"
    home.mkdir()
    rec = replace(_rec(tmp_path), data_plane_host="10.0.0.7")
    (home / cluster.INTENT_NAME).write_text(json.dumps({"record": asdict(rec)}))
    assert cluster.get_record(home) == rec
    # A record with no `data_plane_host` key at all loads with the "" default.
    old_shape = asdict(rec)
    del old_shape["data_plane_host"]
    (home / cluster.INTENT_NAME).write_text(json.dumps({"record": old_shape}))
    loaded = cluster.get_record(home)
    assert loaded is not None
    assert loaded.data_plane_host == ""


def test_url_host_reads_host_with_loopback_fallback():
    """`url_host` — the one host-from-URL read the A5 dial sites share: hostname
    when present, 127.0.0.1 when the URL carries none (a defensive floor for a
    hand-written URL; every generated data-plane URL always names a host)."""
    from base.host.net.url_secret import url_host

    assert url_host("postgresql://x@127.0.0.1:5433/postgres") == "127.0.0.1"
    assert url_host("redis://ava:p@10.0.0.7:6380/0") == "10.0.0.7"
    assert url_host("postgresql://x@/postgres") == "127.0.0.1"
    assert url_host("redis://:6380/0") == "127.0.0.1"


# ── S4 isolation: health-port tables single-sourced + late-slot fallback ──


def test_health_port_tables_in_sync():
    """Guard: the descriptions of "daemon health ports" must name the same
    service set, or a new daemon can silently fall out of the derive surface
    (im_bridge / delivery_watchdog did exactly that: settings and DEFAULT_PORTS
    knew them, the env-var derive surface did not).

    The tables are derived from `_HEALTH_PORT_SERVICES` and the fixed port table
    rather than hand-maintained; this test pins the derivation so a future hand
    edit is a test failure, not a silent drift."""
    from base.daemon import health
    from base.host.env.port_table import FIXED_PORTS

    svcs = set(registry.health_port_env_aliases())
    assert set(health._HEALTH_PORT_OVERRIDES) == svcs
    assert set(health.DEFAULT_PORTS) == svcs
    # every health daemon has a slot in the fixed table
    assert svcs <= set(FIXED_PORTS)
    # the fallback is the FIXED_PORTS subset, by construction
    assert {svc: FIXED_PORTS[svc] for svc in svcs} == health.DEFAULT_PORTS
    # env vars follow the AVA_<NAME>_HEALTH_PORT shape — a rename elsewhere
    # (settings alias, dotenv_boot force set) breaks this loudly
    for svc, var in registry.health_port_env_aliases().items():
        assert var == f"AVA_{svc.upper()}_HEALTH_PORT", f"{svc} -> {var}"
    # every health var is part of the derive surface (subprocess strip + force)
    assert set(registry.health_port_env_aliases().values()) <= registry.derived_env_keys()


@pytest.mark.parametrize("missing", ["redis_admin_password", "redis_password"])
def test_derive_authenticated_env_refuses_missing_data_plane_credential(
    tmp_path: Path, missing: str
) -> None:
    credentials = {"redis_admin_password": "admin-value", "redis_password": "runtime-value"}
    credentials[missing] = ""
    with pytest.raises(ValueError, match="explicit data-plane credentials"):
        cluster.derive_env(
            _rec(tmp_path),
            base_db_url="postgresql://ava@localhost:5432/ava",
            base_redis_url="redis://localhost:6379/0",
            cluster_secret="bearer-only",  # noqa: S106 — isolated test credential
            redis_admin_password=credentials["redis_admin_password"],
            redis_password=credentials["redis_password"],
        )
