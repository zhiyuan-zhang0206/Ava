from pathlib import Path
from typing import cast

import pytest

from shared import cluster, env_registry
from shared.port_block import PORT_OFFSETS


def _rec(tmp_path: Path):
    return cluster.ClusterRecord(
        ports={
            "gateway": 18000,
            "frontend": 18001,
            "heartbeat": 18002,
            "restarter": 18003,
            "labeler": 18004,
            "task_maintenance": 18005,
            "memory_indexer": 18006,
            "ops": 18007,
            "milvus": 18008,
            "browser": 18009,
            "permissions_helper": 18010,
            "postgres": 18011,
            "redis": 18012,
            "events_maintenance": 18014,
            # Post-S4-slot birth shape: every PORT_OFFSETS service carries a key.
            "delivery_watchdog": 18016,
            "im_bridge": 18017,
            "agent_host": 18019,
            "pg_backup": 18021,
            "pitr_uploader": 18022,
            "pitr_base_backup": 18023,
            "gateway_watchdog": 18025,
            "agent_runner_watchdog": 18026,
        },
        gateway_home=str(tmp_path / ".ava-t1"),
        created_at="x",
    )


def _old_rec(tmp_path: Path):
    """A record born before the im_bridge/delivery_watchdog/agent_host slots
    existed — the shape EVERY existing registry record has until re-birth. Such
    a unit's .env also predates the keys, so its daemons bind the legacy
    fallback, not a block offset (record_health_port's late-slot rule)."""
    ports = dict(_rec(tmp_path).ports)
    del ports["delivery_watchdog"]
    del ports["im_bridge"]
    del ports["agent_host"]
    del ports["pg_backup"]
    del ports["pitr_uploader"]
    del ports["pitr_base_backup"]
    del ports["gateway_watchdog"]
    del ports["agent_runner_watchdog"]
    return cluster.ClusterRecord(
        ports=cast("cluster.ClusterPorts", ports),
        gateway_home=str(tmp_path / ".ava-t1"),
        created_at="x",
    )


def test_derive_env_ports_and_urls(tmp_path: Path):
    env = cluster.derive_env(
        _rec(tmp_path),
        base_db_url="postgresql://ava:p@localhost:5432/ava",
        base_redis_url="redis://localhost:6379/0",
        cluster_secret="sekret",  # noqa: S106 — test fixture, not a real secret
        pgbouncer_enabled=False,  # pooling off -> AVA_DB_URL stays on the direct pg port
    )
    # the secret is written into the cluster .env so every cluster process has it
    assert env["AVA_CLUSTER_SECRET"] == "sekret"  # noqa: S105 — test fixture, not a real secret
    assert env["AVA_GATEWAY_PORT"] == "18000"
    # a gateway box reaches its own gateway over loopback (self-call); the address
    # remote runners dial is handed out at enroll, never stored here
    assert env["AVA_GATEWAY_URL"] == "http://localhost:18000"
    assert env["AVA_GATEWAY_HEALTH_URL"] == "http://localhost:18000/api/health"
    assert env["AVA_FRONTEND_HEALTHCHECK_URL"] == "http://localhost:18001"
    assert env["AVA_MILVUS_PORT"] == "18008"
    assert env["AVA_MILVUS_URI"] == "http://127.0.0.1:18008"
    # db_url + redis_url carry the data-plane identity AS DATA: a fresh birth
    # writes the fixed `ava` db/role/ACL identifier, password = the cluster
    # secret. The redis URL keeps the base logical DB (0) — every cluster owns
    # its redis, so there is no per-cluster index swap.
    assert env["AVA_DB_URL"] == "postgresql://ava:sekret@localhost:5432/ava"
    assert env["AVA_REDIS_URL"] == "redis://ava:sekret@localhost:6379/0"
    # channels are fixed (single per-cluster redis, no neighbour to prefix away from)
    assert env["AVA_EVENTS_CHANNEL"] == "ava:events"


def test_derive_env_empty_secret_writes_identity_without_password(tmp_path: Path):
    """A no-secret cluster's URLs carry the data-plane identity username but no
    password — names-as-data holds with or without auth, and `identity_from_url`
    (which requires a username) keeps working."""
    env = cluster.derive_env(
        _rec(tmp_path),
        base_db_url="postgresql://ava:p@localhost:5432/ava",
        base_redis_url="redis://localhost:6379/0",
        cluster_secret="",
        pgbouncer_enabled=False,
    )
    assert env["AVA_CLUSTER_SECRET"] == ""
    assert env["AVA_DB_URL"] == "postgresql://ava@localhost:5432/ava"
    assert env["AVA_REDIS_URL"] == "redis://ava@localhost:6379/0"
    # the identities stay readable as data
    from urllib.parse import urlsplit

    assert urlsplit(env["AVA_DB_URL"]).username == "ava"
    assert urlsplit(env["AVA_REDIS_URL"]).username == "ava"


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
    )
    assert set(env) == env_registry.derived_env_keys()
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
    )
    from urllib.parse import urlsplit

    assert urlsplit(env["AVA_DB_URL"]).port == 18000 + PORT_OFFSETS["pgbouncer"]  # 18013
    assert "AVA_PGBOUNCER_PORT" not in env


def test_per_cluster_base_urls_point_at_own_instance_ports(tmp_path: Path):
    """Every cluster's base URLs are loopback at its own allocated pg/redis ports —
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


def test_registry_round_trip_preserves_data_plane_host(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """`data_plane_host` is a durable registry fact: save → load returns it, and
    an old-shape record (no field on disk) loads with the loopback default — the
    compat rule every existing cluster depends on."""
    import json
    from dataclasses import replace

    from shared import cluster as cl

    reg = tmp_path / "clusters.json"
    monkeypatch.setattr(cl, "registry_path", lambda: reg)
    rec = replace(_rec(tmp_path), data_plane_host="10.0.0.7")
    cl.save_record(rec)
    assert cl.get_record(tmp_path / ".ava-t1") == rec
    # Old-shape on-disk record without the field loads with the "" default.
    reg.write_text(
        json.dumps(
            {
                ".ava-t1": {
                    "name": ".ava-t1",
                    "ports": dict(rec.ports),
                    "gateway_home": str(tmp_path / ".ava-t1"),
                    "created_at": "x",
                }
            }
        )
    )
    loaded = cl.get_record(tmp_path / ".ava-t1")
    assert loaded is not None
    assert loaded.data_plane_host == ""


def test_url_host_reads_host_with_loopback_fallback():
    """`url_host` — the one host-from-URL read the A5 dial sites share: hostname
    when present, 127.0.0.1 when the URL carries none (a defensive floor for a
    hand-written URL; every generated data-plane URL always names a host)."""
    from shared.url_secret import url_host

    assert url_host("postgresql://x@127.0.0.1:5433/postgres") == "127.0.0.1"
    assert url_host("redis://ava:p@10.0.0.7:6380/0") == "10.0.0.7"
    assert url_host("postgresql://x@/postgres") == "127.0.0.1"
    assert url_host("redis://:6380/0") == "127.0.0.1"


def test_wsl_default_health_port_base_cannot_collide_with_a_birthed_cluster():
    """`env_registry.WSL_DEFAULT_HEALTH_PORT_BASE` (issue #1152) must sit outside the
    birth allocator's own scan range — otherwise a cluster later born on the same
    WSL2 box (`cluster.allocate_ports`, which scans [BLOCK_START, BLOCK_MAX))
    could eventually claim the exact base a WSL2 unit auto-defaulted to,
    recreating the collision this constant exists to avoid."""
    from shared import port_block

    assert env_registry.WSL_DEFAULT_HEALTH_PORT_BASE >= port_block.BLOCK_MAX


def test_wsl_default_health_port_base_derives_a_legal_block():
    """The reserved base must itself produce ports inside 1024-65535 — asserted
    directly rather than assumed, since `health_port_env` raises otherwise."""
    ports = env_registry.health_port_env(env_registry.WSL_DEFAULT_HEALTH_PORT_BASE)
    assert all(1024 <= int(p) <= 65535 for p in ports.values())


def test_seed_keys_disjoint_from_cluster_identity():
    """The install-time seed allowlist must never overlap the cluster-isolation /
    machine-identity key sets — a seeded worktree copies capability credentials
    (and the endpoint they are minted against) only, never the prod data plane,
    serve flags, or the cluster secret (always minted fresh). AVA_TELEGRAM_BOT_TOKEN is additionally banned by name: it is in
    neither set, but two live clusters polling one bot token fight over the same
    getUpdates long-poll."""
    overlap = env_registry.seed_allowlist() & (
        env_registry.derived_env_keys() | env_registry.env_identity_keys()
    )
    assert not overlap, f"seed_allowlist() leaks identity/derived keys: {sorted(overlap)}"
    assert "AVA_CLUSTER_SECRET" not in env_registry.seed_allowlist()
    assert "AVA_TELEGRAM_BOT_TOKEN" not in env_registry.seed_allowlist()
    assert env_registry.seed_allowlist(), "seed allowlist must not be empty"


def test_seed_keys_are_declared_by_settings_or_enabled_provider_plugin():
    """Every seed key is a Settings alias or an enabled provider binding.

    Provider bindings are intentionally the declaration for plugin keys: making
    them Settings fields would make a removable plugin widen core config.
    """
    from shared.config import FIELD_INFOS

    aliases = {
        field.serialization_alias or field.alias or name.upper()
        for name, field in FIELD_INFOS.items()
    }
    plugin_keys = env_registry._enabled_provider_key_envs()
    missing = sorted(env_registry.seed_allowlist() - aliases - plugin_keys)
    assert not missing, f"seed_allowlist entries with no declaration: {missing}"


def test_health_port_env_derives_the_block_from_a_base():
    """`--health-port-base` lands each daemon exactly where an allocated cluster
    would put it — base + the service's block offset, not a second convention.

    Pinned against `derive_env` on a record with the same base, so the operator's
    hand-set unit and an installed cluster can never diverge on layout."""
    from shared.port_block import PORT_OFFSETS

    derived = env_registry.health_port_env(18000)
    assert derived == {
        var: str(18000 + PORT_OFFSETS[svc])
        for svc, var in env_registry.health_port_env_aliases().items()
    }


def test_health_port_env_matches_derive_env_for_the_same_base(tmp_path: Path):
    """The two producers of an `AVA_*_HEALTH_PORT` set agree value-for-value.

    `derive_env` writes a cluster's own block at install; `health_port_env` writes
    a unit's block at enroll. Two code paths, one layout — a drift would put a
    co-located unit's ports somewhere the operator did not read off `ava cluster ls`."""
    installed = cluster.derive_env(
        _rec(tmp_path),
        base_db_url="postgresql://ava:p@localhost:5432/ava",
        base_redis_url="redis://localhost:6379/0",
        cluster_secret="sekret",  # noqa: S106 — test fixture, not a real secret
    )
    hand_set = env_registry.health_port_env(18000)
    assert {k: installed[k] for k in hand_set} == hand_set


def test_health_port_env_refuses_a_base_that_overflows_the_port_range():
    """A base whose block runs past 65535 is a typo, and clamping it would bind
    ports nobody asked for — so it raises instead."""
    import pytest

    with pytest.raises(ValueError, match="outside 1024-65535"):
        env_registry.health_port_env(65530)


# ── S4 isolation: health-port tables single-sourced + late-slot fallback ──


def test_health_port_tables_in_sync():
    """F-s4-3 guard: the four descriptions of "per-unit daemon health ports" must
    name the same service set, or a new daemon can silently fall out of the
    per-unit model (im_bridge / delivery_watchdog did exactly that: settings and
    DEFAULT_PORTS knew them, the env-var derive surface did not, so
    `--health-port-base` did not move them).

    The tables are now derived from PORT_OFFSETS + _HEALTH_PORT_ENV rather than
    hand-maintained; this test pins the derivation so a future hand edit is a
    test failure, not a silent drift."""
    from shared import daemon_health
    from shared.port_block import LEGACY_AVA_PORTS, PORT_OFFSETS

    svcs = set(env_registry.health_port_env_aliases())
    assert set(daemon_health._HEALTH_PORT_OVERRIDES) == svcs
    assert set(daemon_health.DEFAULT_PORTS) == svcs
    # every health daemon lives in the block table (offsets 16/17 for the two
    # late daemons), so enroll / derive / preflight all move it
    assert svcs <= set(PORT_OFFSETS)
    # the legacy fallback is the LEGACY_AVA_PORTS subset, by construction
    assert {svc: LEGACY_AVA_PORTS[svc] for svc in svcs} == daemon_health.DEFAULT_PORTS
    # env vars follow the AVA_<NAME>_HEALTH_PORT shape — a rename elsewhere
    # (settings alias, dotenv_boot force set) breaks this loudly
    for svc, var in env_registry.health_port_env_aliases().items():
        assert var == f"AVA_{svc.upper()}_HEALTH_PORT", f"{svc} -> {var}"
    # every health var is part of the derive surface (subprocess strip + force)
    assert set(env_registry.health_port_env_aliases().values()) <= env_registry.derived_env_keys()


def test_derive_env_old_record_health_ports_fall_back_to_legacy(tmp_path: Path):
    """A record born before the im_bridge/delivery_watchdog slots (EVERY
    existing record) derives the legacy ports for them — the unit's .env also
    predates the keys, so its daemons bind the shared default, never a block
    offset. derive_env must write what the daemon will actually bind, or the
    .env and the record disagree on day one."""
    installed = cluster.derive_env(
        _old_rec(tmp_path),
        base_db_url="postgresql://ava:p@localhost:5432/ava",
        base_redis_url="redis://localhost:6379/0",
        cluster_secret="sekret",  # noqa: S106 — test fixture, not a real secret
    )
    from shared.port_block import LEGACY_AVA_PORTS

    assert installed["AVA_DELIVERY_WATCHDOG_HEALTH_PORT"] == str(
        LEGACY_AVA_PORTS["delivery_watchdog"]
    )
    assert installed["AVA_IM_BRIDGE_HEALTH_PORT"] == str(LEGACY_AVA_PORTS["im_bridge"])
    assert installed["AVA_PG_BACKUP_HEALTH_PORT"] == str(LEGACY_AVA_PORTS["pg_backup"])
    # the 7 original daemons keep their block offsets (the record carries them)
    assert installed["AVA_RESTARTER_HEALTH_PORT"] == "18003"


def test_record_health_port_late_slot_legacy_for_old_record(tmp_path: Path):
    """record_health_port: a missing im_bridge/delivery_watchdog key derives the
    legacy value (matching the runtime fallback), while a post-slot record
    returns its own key — the preflight map can never name a port nothing binds."""
    from shared.port_block import LEGACY_AVA_PORTS

    assert (
        cluster.record_health_port(_old_rec(tmp_path), "im_bridge") == LEGACY_AVA_PORTS["im_bridge"]
    )
    assert (
        cluster.record_health_port(_old_rec(tmp_path), "delivery_watchdog")
        == (LEGACY_AVA_PORTS["delivery_watchdog"])
    )
    assert (
        cluster.record_health_port(_old_rec(tmp_path), "pg_backup") == LEGACY_AVA_PORTS["pg_backup"]
    )
    assert (
        cluster.record_health_port(_old_rec(tmp_path), "gateway_watchdog")
        == LEGACY_AVA_PORTS["gateway_watchdog"]
    )
    assert (
        cluster.record_health_port(_old_rec(tmp_path), "agent_runner_watchdog")
        == LEGACY_AVA_PORTS["agent_runner_watchdog"]
    )
    assert cluster.record_health_port(_rec(tmp_path), "im_bridge") == 18017
    assert cluster.record_health_port(_rec(tmp_path), "pg_backup") == 18021
    assert cluster.record_health_port(_rec(tmp_path), "gateway_watchdog") == 18025
    assert cluster.record_health_port(_rec(tmp_path), "agent_runner_watchdog") == 18026
    # pre-existing slots keep the base+offset derive for old records
    assert cluster.record_health_port(_old_rec(tmp_path), "heartbeat") == 18002


def test_record_health_port_agent_host_never_reaches_past_the_allocated_block(tmp_path: Path):
    """The hosted agent-runner's offset (19) is the first one that lands OUTSIDE
    the block an existing record owns: those records were allocated at
    BLOCK_SIZE 19, so they hold base..base+18, and `base + 19` is the FIRST PORT
    OF THE NEXT CLUSTER'S BLOCK. Growing the block must not make an existing
    cluster's own derive point at a neighbour, so `agent_host` derives its
    legacy value — which is also what `health_port()` binds at runtime for a
    unit whose `.env` predates the key.

    A cluster BORN after this change allocates a 20-port block and carries the
    key, so it reads its own port back — the contrast the second half pins."""
    from shared.port_block import LEGACY_AVA_PORTS

    old = _old_rec(tmp_path)
    derived = cluster.record_health_port(old, "agent_host")
    assert derived == LEGACY_AVA_PORTS["agent_host"]
    # The number the block-offset derive would have produced, and the reason it
    # is wrong: it is the first port of whoever holds the next block.
    assert derived != old.ports["gateway"] + 19
    # A fresh birth carries the key and reads it back — inside its own block.
    assert cluster.record_health_port(_rec(tmp_path), "agent_host") == 18019


def test_allocate_ports_skips_blocks_overlapping_legacy_16_port_records(
    monkeypatch: pytest.MonkeyPatch,
):
    """BLOCK_SIZE has grown repeatedly through 24, but every
    pre-existing record still occupies a 16-port block at 18000+16k — and a
    candidate inside such a block would overlap it. allocate_ports must skip
    overlapping blocks, not just exact bases, or a DOWN cluster's block gets
    re-allocated while its record still owns it (silent collision when both
    start).

    The expected base below is concrete on purpose and MOVES whenever
    BLOCK_SIZE does: growing the block changes which candidates clear a legacy
    record, so a block growth should force someone to re-check allocation
    rather than slide past a derived assertion."""
    from shared import cluster as cl
    from shared.port_block import BLOCK_SIZE, BLOCK_START

    monkeypatch.setattr(cl, "_port_free", lambda _port: True)  # pyright: ignore[reportUnknownArgumentType]

    # Existing record at 18016 occupies 18016..18031. At BLOCK_SIZE 27,
    # candidates 18000 and 18027 overlap it; the first legal base is 18054.
    ports = cl.allocate_ports({18016})
    assert ports["gateway"] == 18054
    assert set(ports) == set(cl.PORT_OFFSETS)
    # without any existing record, the allocator starts at BLOCK_START
    assert cl.allocate_ports(set())["gateway"] == BLOCK_START
    # an exact-base record is of course skipped too
    assert cl.allocate_ports({BLOCK_START})["gateway"] == BLOCK_START + BLOCK_SIZE
