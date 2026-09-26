from pathlib import Path
from typing import cast

import pytest

from shared import cluster, port_preflight
from shared.platform import LockTimeoutError, file_lock


def test_registry_lock_contention_has_a_bounded_wait(tmp_path: Path) -> None:
    registry = tmp_path / "clusters.json"
    with (
        file_lock(registry.with_suffix(".lock"), timeout_s=1),
        pytest.raises(LockTimeoutError),
        cluster.registry_lock(path=registry, timeout_s=0.02),
    ):
        pytest.fail("A contended registry lock must not be entered")


def test_save_and_get_record(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    reg = tmp_path / "clusters.json"
    monkeypatch.setattr(cluster, "registry_path", lambda: reg)
    home = tmp_path / ".ava-t1"
    rec = cluster.ClusterRecord(
        ports=cast("cluster.ClusterPorts", {"gateway": 18000, "frontend": 18001}),
        gateway_home=str(home),
        created_at="2026-06-01T00:00:00Z",
    )
    cluster.save_record(rec)
    got = cluster.get_record(home)
    assert got == rec
    assert cluster.get_record(tmp_path / "missing") is None


def test_load_registry_rekeys_legacy_name_keyed_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """A registry written pre-path-only is keyed by cluster NAME and carries
    retired fields (`name` / `db_name` / `redis_db_index` / `redis_prefix`).
    load_registry re-keys rows in memory by their own gateway_home and drops
    the fields the dataclass no longer declares, instead of crashing on
    ClusterRecord(**v)."""
    import json

    reg = tmp_path / "clusters.json"
    reg.write_text(
        json.dumps(
            {
                "main": {
                    "name": "main",
                    "db_name": "ava_main",
                    "redis_db_index": 0,
                    "redis_prefix": "ava",
                    "ports": {"gateway": 8000, "postgres": 5433, "redis": 6380},
                    "gateway_home": "/home/x/.ava",
                    "created_at": "2026-06-01T00:00:00Z",
                }
            }
        )
    )
    monkeypatch.setattr(cluster, "registry_path", lambda: reg)
    rec = cluster.get_record(Path("/home/x/.ava"))
    assert rec is not None
    assert rec.gateway_home == "/home/x/.ava"
    assert rec.ports["postgres"] == 5433
    # Retired fields (never declared by the current dataclass) are dropped.
    assert not hasattr(rec, "name")
    assert not hasattr(rec, "redis_db_index")


def test_delete_record_by_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    reg = tmp_path / "clusters.json"
    monkeypatch.setattr(cluster, "registry_path", lambda: reg)
    home = tmp_path / ".ava-t2"
    cluster.save_record(
        cluster.ClusterRecord(
            ports=cast("cluster.ClusterPorts", {"gateway": 18016}),
            gateway_home=str(home),
            created_at="t",
        )
    )
    assert cluster.delete_record(home) is True
    assert cluster.get_record(home) is None
    assert cluster.delete_record(home) is False


def test_load_registry_accepts_mixed_name_and_home_keys(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """A half-migrated file (one legacy name-keyed row + one home-keyed row)
    loads to two home-keyed records — every row is re-keyed by its own
    gateway_home regardless of the JSON key shape."""
    import json

    reg = tmp_path / "clusters.json"
    reg.write_text(
        json.dumps(
            {
                "t1": {
                    "name": "t1",
                    "ports": {"gateway": 18000},
                    "gateway_home": "/h/.ava-t1",
                    "created_at": "t",
                },
                "/h/.ava-t2": {
                    "ports": {"gateway": 18016},
                    "gateway_home": "/h/.ava-t2",
                    "created_at": "t",
                },
            }
        )
    )
    monkeypatch.setattr(cluster, "registry_path", lambda: reg)
    loaded = cluster.load_registry()
    assert set(loaded) == {"/h/.ava-t1", "/h/.ava-t2"}


def test_load_registry_refuses_two_records_for_one_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """Two records claiming one gateway_home would silently last-win (and the
    converge migration would persist the loss, freeing a possibly-live port
    block) — load must refuse, naming both source keys."""
    import json

    import pytest

    reg = tmp_path / "clusters.json"
    reg.write_text(
        json.dumps(
            {
                "t1": {
                    "name": "t1",
                    "ports": {"gateway": 18000},
                    "gateway_home": "/h/.ava-dup",
                    "created_at": "t",
                },
                "/h/.ava-dup": {
                    "ports": {"gateway": 18016},
                    "gateway_home": "/h/.ava-dup",
                    "created_at": "t",
                },
            }
        )
    )
    monkeypatch.setattr(cluster, "registry_path", lambda: reg)
    with pytest.raises(RuntimeError, match="two records claiming home"):
        cluster.load_registry()


def test_load_registry_refuses_record_without_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """The home path IS the identity — a record with no gateway_home is
    unaddressable and must fail loudly, not load as an empty-keyed row."""
    import json

    import pytest

    reg = tmp_path / "clusters.json"
    reg.write_text(
        json.dumps({"t1": {"ports": {"gateway": 18000}, "gateway_home": "", "created_at": "t"}})
    )
    monkeypatch.setattr(cluster, "registry_path", lambda: reg)
    with pytest.raises(RuntimeError, match="no gateway_home"):
        cluster.load_registry()


def test_load_registry_empty_when_no_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setattr(cluster, "registry_path", lambda: tmp_path / "nope.json")
    assert cluster.load_registry() == {}


# --- port preflight helpers (issue: ava start port preflight) ---


def test_expected_cluster_ports_reads_the_full_block_from_record(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """A record carrying the full block resolves the full service->port map:
    the record IS the block for an allocated cluster (stored keys win)."""
    home = tmp_path / ".ava-t1"
    rec = cluster.ClusterRecord(
        ports=cast(
            "cluster.ClusterPorts",
            {
                "gateway": 18032,
                "frontend": 18033,
                "heartbeat": 18034,
                "restarter": 18035,
                "labeler": 18036,
                "task_maintenance": 18037,
                "memory_indexer": 18038,
                "ops": 18039,
                "milvus": 18040,
                "browser": 18041,
                "permissions_helper": 18042,
                "postgres": 18043,
                "redis": 18044,
                "pgbouncer": 18045,
                "events_maintenance": 18046,
                "app": 18047,
                "delivery_watchdog": 18048,
                "im_bridge": 18049,
                "page_server": 18050,
                "agent_host": 18051,
                "pg_backup": 18053,
                "pitr_uploader": 18054,
                "pitr_base_backup": 18055,
                "memory_search": 18056,
                "gateway_watchdog": 18057,
                "agent_runner_watchdog": 18058,
            },
        ),
        gateway_home=str(home),
        created_at="2026-07-01T00:00:00Z",
    )
    monkeypatch.setattr(port_preflight, "get_record", lambda _home: rec)  # pyright: ignore[reportUnknownArgumentType]

    ports = dict(port_preflight.expected_cluster_ports(home))
    assert ports["gateway"] == 18032 and ports["app"] == 18047
    assert ports["agent_host"] == 18051 and ports["agent_runner_watchdog"] == 18058


def test_expected_cluster_ports_missing_key_raises(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """An allocated record missing a block key is corrupt — the expected-port
    map refuses to guess a neighbour's port (records are born with the full
    block; only the default home may fall back)."""
    home = tmp_path / ".ava-t1"
    rec = cluster.ClusterRecord(
        ports=cast("cluster.ClusterPorts", {"gateway": 18032, "frontend": 18033}),
        gateway_home=str(home),
        created_at="2026-07-01T00:00:00Z",
    )
    monkeypatch.setattr(port_preflight, "get_record", lambda _home: rec)  # pyright: ignore[reportUnknownArgumentType]
    with pytest.raises(KeyError):
        port_preflight.expected_cluster_ports(home)


def test_expected_cluster_ports_falls_back_to_legacy_without_record(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """A record-less default home (a pre-registry install) binds the fixed legacy
    block — its ports ARE its record."""
    monkeypatch.setattr(port_preflight, "get_record", lambda _home: None)  # pyright: ignore[reportUnknownArgumentType]
    assert port_preflight.expected_cluster_ports(tmp_path / "x") == port_preflight.LEGACY_AVA_PORTS


def test_occupied_ports_reports_bound_and_exempts_ours(monkeypatch: pytest.MonkeyPatch):
    """Bind-check per port; `is_ours` lets a caller exempt the listeners it can
    identify as its own (an idempotent restart's own daemons)."""
    taken = {8000, 5433}

    def fake_port_free(port: int) -> bool:
        return port not in taken

    monkeypatch.setattr(port_preflight, "_port_free", fake_port_free)
    ports = {"gateway": 8000, "frontend": 3000, "postgres": 5433}

    assert port_preflight.occupied_ports(ports) == [("gateway", 8000), ("postgres", 5433)]
    assert port_preflight.occupied_ports(ports, is_ours=lambda p: p == 8000) == [("postgres", 5433)]
    assert port_preflight.occupied_ports(ports, is_ours=lambda p: p in taken) == []


def test_env_port_drift_reports_mismatched_keys_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """`.env` port keys that disagree with the record are drift lines; matching
    keys and absent keys are not. Health ports are excluded (per-unit fact)."""
    home = tmp_path / ".ava-main"
    home.mkdir(parents=True)
    (home / ".env").write_text(
        "AVA_GATEWAY_PORT=8001\n"
        "AVA_MILVUS_PORT=19530\n"
        # Pooling OFF in this fixture's .env: with the toggle on (default) the
        # expected AVA_DB_URL port is the pooler listener, so a direct-port URL
        # would read as drift (see test_env_port_drift_pooled_url_expects_pooler_port).
        "AVA_PGBOUNCER_ENABLED=false\n"
        "AVA_DB_URL=postgresql://ava:sek@127.0.0.1:5433/ava\n"
        "AVA_REDIS_URL=redis://ava:sek@127.0.0.1:9999/0\n"
        "AVA_RESTARTER_HEALTH_PORT=9999\n"
    )
    rec = cluster.ClusterRecord(
        ports=cast(
            "cluster.ClusterPorts",
            {
                "gateway": 8000,
                "frontend": 3000,
                "app": 3001,
                "milvus": 19530,
                "browser": 9222,
                "permissions_helper": 9223,
                "pgbouncer": 6433,
                "postgres": 5433,
                "redis": 6380,
            },
        ),
        gateway_home=str(home),
        created_at="2026-06-08T00:00:00Z",
    )
    drift = port_preflight.env_port_drift(home, rec)
    assert drift == [
        "AVA_GATEWAY_PORT: .env='8001' vs registry=8000",
        "AVA_REDIS_URL: url port=9999 vs registry=6380",
    ]


def test_env_port_drift_pooled_url_expects_pooler_port(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """The one-URL design: with pooling on (the default), a pre-cutover direct-port
    AVA_DB_URL IS drift (converge normalizes it to the pooler port), while the
    pooler-port URL matches. A URL naming neither port is left alone."""
    from shared import port_preflight as pp

    # The record's home is the default home so the pooler port derives the
    # legacy 6433 (an allocated-cluster record would derive base+13).
    home = tmp_path / ".ava-pooled"
    home.mkdir(parents=True)
    rec = cluster.ClusterRecord(
        ports=cast(
            "cluster.ClusterPorts",
            {"gateway": 8000, "frontend": 3000, "app": 3001, "postgres": 5433, "redis": 6380},
        ),
        gateway_home=str(cluster.default_home()),
        created_at="t",
    )
    # no AVA_PGBOUNCER_ENABLED -> default on -> pooler 6433 expected
    (home / ".env").write_text(
        "AVA_DB_URL=postgresql://ava:sek@127.0.0.1:5433/ava\n"  # pre-cutover direct port
    )
    assert pp.env_port_drift(home, rec) == ["AVA_DB_URL: url port=5433 vs registry=6433"]

    (home / ".env").write_text(
        "AVA_DB_URL=postgresql://ava:sek@127.0.0.1:6433/ava\n"  # normalized pooler port
    )
    assert pp.env_port_drift(home, rec) == []

    # an operator stand-in naming neither port still warns (it dials something
    # the registry does not own) — converge leaves it alone, the warning does not
    # (the strict compare matches the pre-F8b behavior for off-cluster ports).
    (home / ".env").write_text("AVA_DB_URL=postgresql://ava:dev@localhost:5432/ava\n")
    assert pp.env_port_drift(home, rec) == ["AVA_DB_URL: url port=5432 vs registry=6433"]


def test_registry_disk_form_is_home_keyed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """The on-disk registry is keyed by home path — the record identity — so
    two records can never collide on a name key."""
    reg = tmp_path / "clusters.json"
    monkeypatch.setattr(cluster, "registry_path", lambda: reg)
    reg_map = {
        "/home/x/.ava-t1": cluster.ClusterRecord(
            ports=cast("cluster.ClusterPorts", {"gateway": 18000, "frontend": 18001}),
            gateway_home="/home/x/.ava-t1",
            created_at="2026-06-01T00:00:00Z",
        ),
        "/home/x/.ava-t2": cluster.ClusterRecord(
            ports=cast("cluster.ClusterPorts", {"gateway": 18032, "frontend": 18033}),
            gateway_home="/home/x/.ava-t2",
            created_at="2026-06-01T00:00:00Z",
        ),
    }
    disk = cluster._registry_disk_form(reg_map)
    assert set(disk) == {"/home/x/.ava-t1", "/home/x/.ava-t2"}


def test_unit_port_map_overlays_health_ports(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """unit_port_map = the cluster's port block overlaid by this unit's health
    ports (the per-unit layer `ava enroll --health-port-base` moves). This is
    the exact set the start preflight scans and `ava stop`'s orphan sweep
    reaps (Task #965) — one composition, two consumers."""
    import shared.daemon_health as _dh
    import shared.port_preflight as pp

    monkeypatch.setattr(pp, "get_record", lambda _home: None)  # pyright: ignore[reportUnknownArgumentType]
    base = 18100
    # unit_port_map reads health_port from shared.daemon_health (lazy import)
    monkeypatch.setattr(_dh, "health_port", lambda svc: base + len(svc))  # pyright: ignore[reportUnknownArgumentType]

    ports = pp.unit_port_map(tmp_path / "x")

    # block keys present (legacy fallback for a record-less home)
    assert ports["gateway"] == 8000 and ports["postgres"] == 5433
    # health ports overlay the block for the daemons that have them
    assert ports["agent_host"] == base + len("agent_host")
    assert ports["restarter"] == 8102  # Retired slot stays reserved, never a live probe.
    assert ports["ops"] == base + len("ops")
