from pathlib import Path
from typing import cast

import pytest

from shared import cluster, port_preflight


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
    `name` / `db_name` (which path-only KEEPS as compat passthrough) plus
    truly-retired fields (`redis_db_index` / `redis_prefix`, dropped).
    load_registry re-keys rows in memory by their own gateway_home while
    preserving the compat fields instead of crashing on ClusterRecord(**v)."""
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
    # Compat fields are PRESERVED (a box-shared pre-cutover reader needs them).
    assert rec.name == "main"
    assert rec.db_name == "ava_main"
    # Truly-retired fields the dataclass never declared are still dropped.
    assert not hasattr(rec, "redis_db_index")


def test_migrate_registry_keys_keeps_backward_compatible_form(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """The converge migration normalizes to the migration-window form: the file
    stays NAME-keyed and every record carries name/db_name (a box-shared
    pre-cutover reader looks records up by name and REQUIRES those fields).
    Home-keying + dropping them is the future contract step, NOT this."""
    import json

    reg = tmp_path / "clusters.json"
    reg.write_text(
        json.dumps(
            {
                "t1": {
                    "name": "t1",
                    "db_name": "ava_t1",
                    "redis_prefix": "ava",  # truly-retired: dropped
                    "ports": {"gateway": 18000},
                    "gateway_home": "/home/x/.ava-t1",
                    "created_at": "2026-06-01T00:00:00Z",
                }
            }
        )
    )
    monkeypatch.setattr(cluster, "registry_path", lambda: reg)
    assert cluster.migrate_registry_keys() is True  # drops redis_prefix
    on_disk = json.loads(reg.read_text())
    # Still name-keyed, NOT home-keyed.
    assert set(on_disk) == {"t1"}
    assert on_disk["t1"]["name"] == "t1"
    assert on_disk["t1"]["db_name"] == "ava_t1"
    assert on_disk["t1"]["gateway_home"] == "/home/x/.ava-t1"
    assert "redis_prefix" not in on_disk["t1"]
    assert cluster.migrate_registry_keys() is False  # second run: already normalized


def test_migrate_repairs_home_keyed_file_missing_compat_fields(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """The incident: a buggy path-only build rewrote the shared file to HOME keys
    and DROPPED name/db_name. A box-shared pre-cutover reader then crashes with
    `TypeError: missing name/db_name`. migrate must REPAIR it — back to name keys
    with the compat fields backfilled (synthesized for the nameless record)."""
    import json

    reg = tmp_path / "clusters.json"
    reg.write_text(
        json.dumps(
            {
                "/home/x/.ava-t1": {  # home-keyed, no name/db_name — the corruption
                    "ports": {"gateway": 18000},
                    "gateway_home": "/home/x/.ava-t1",
                    "created_at": "2026-06-01T00:00:00Z",
                }
            }
        )
    )
    monkeypatch.setattr(cluster, "registry_path", lambda: reg)
    assert cluster.migrate_registry_keys() is True
    on_disk = json.loads(reg.read_text())
    # Re-keyed to the synthesized name (home slug), compat fields present.
    (key,) = on_disk
    assert key != "/home/x/.ava-t1"  # no longer home-keyed
    row = on_disk[key]
    assert row["name"] == key and row["name"]  # non-empty synthesized name
    assert row["db_name"] == cluster.DATA_PLANE_IDENTITY
    assert row["gateway_home"] == "/home/x/.ava-t1"


def test_load_save_round_trip_preserves_compat_fields(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """A path-only load→save round-trip must NOT lose name/db_name — otherwise
    every save silently re-corrupts the shared file for a pre-cutover reader."""
    import json

    reg = tmp_path / "clusters.json"
    reg.write_text(
        json.dumps(
            {
                "main": {
                    "name": "main",
                    "db_name": "ava_main",
                    "ports": {"gateway": 8000},
                    "gateway_home": "/home/x/.ava",
                    "created_at": "2026-06-01T00:00:00Z",
                }
            }
        )
    )
    monkeypatch.setattr(cluster, "registry_path", lambda: reg)
    loaded = cluster.get_record(Path("/home/x/.ava"))
    assert loaded is not None
    cluster.save_record(loaded)  # round-trip
    on_disk = json.loads(reg.read_text())
    assert on_disk["main"]["name"] == "main"
    assert on_disk["main"]["db_name"] == "ava_main"


def test_pre_cutover_reader_can_still_parse_the_persisted_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """Minimal replica of the pre-cutover load logic (ClusterRecord REQUIRES
    name + db_name, looked up BY name) — it must construct every record from the
    file path-only code writes, without a TypeError."""
    import json
    from dataclasses import dataclass

    @dataclass(frozen=True)
    class LegacyRecord:  # the pre-path-only required shape
        name: str
        db_name: str
        ports: dict
        gateway_home: str
        created_at: str

    reg = tmp_path / "clusters.json"
    monkeypatch.setattr(cluster, "registry_path", lambda: reg)
    # A path-only birth (no name given) + a preserved legacy record, both saved
    # by path-only code, then read back by the legacy loader.
    cluster.save_record(
        cluster.ClusterRecord(
            ports=cast("cluster.ClusterPorts", {"gateway": 18000}),
            gateway_home="/home/x/.ava-t1",
            created_at="t",
        )
    )
    raw = json.loads(reg.read_text())
    legacy_known = {"name", "db_name", "ports", "gateway_home", "created_at"}
    legacy = {
        k: LegacyRecord(**{kk: vv for kk, vv in v.items() if kk in legacy_known})
        for k, v in raw.items()
    }
    (name,) = legacy  # keyed by name, as the pre-cutover reader expects
    assert legacy[name].name == name
    assert legacy[name].db_name  # non-empty (synthesized)
    assert legacy[name].gateway_home == "/home/x/.ava-t1"


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


def test_expected_cluster_ports_derives_full_block_from_record(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """A record saved before later slots existed still resolves the full 16-port
    block: stored keys win, missing keys derive as base+offset (the record's
    gateway port is the block base) for an allocated cluster."""
    home = tmp_path / ".ava-t1"
    rec = cluster.ClusterRecord(
        ports=cast("cluster.ClusterPorts", {"gateway": 18032, "frontend": 18033}),
        gateway_home=str(home),
        created_at="2026-07-01T00:00:00Z",
    )
    monkeypatch.setattr(port_preflight, "get_record", lambda _home: rec)  # pyright: ignore[reportUnknownArgumentType]

    ports = dict(port_preflight.expected_cluster_ports(home))
    assert ports["gateway"] == 18032 and ports["frontend"] == 18033
    assert ports["heartbeat"] == 18034 and ports["restarter"] == 18035
    assert ports["labeler"] == 18036 and ports["task_maintenance"] == 18037
    assert ports["memory_indexer"] == 18038 and ports["ops"] == 18039
    assert ports["milvus"] == 18040 and ports["browser"] == 18041
    assert ports["permissions_helper"] == 18042
    assert ports["postgres"] == 18043 and ports["redis"] == 18044
    assert ports["pgbouncer"] == 18045 and ports["events_maintenance"] == 18046
    assert ports["app"] == 18047


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


def test_registry_disk_form_refuses_duplicate_names(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """F-s4-8: the on-disk registry is NAME-keyed during the migration window, so
    two records sharing a compat name would silently overwrite one another —
    freeing a port block that may still be in use. The write must refuse, not
    last-win: identity is the home path, and a name collision is a legacy-name
    clash the operator resolves by hand."""
    reg = tmp_path / "clusters.json"
    monkeypatch.setattr(cluster, "registry_path", lambda: reg)

    def rec(home: str, name: str) -> cluster.ClusterRecord:
        return cluster.ClusterRecord(
            ports=cast("cluster.ClusterPorts", {"gateway": 18000, "frontend": 18001}),
            gateway_home=home,
            created_at="2026-06-01T00:00:00Z",
            name=name,
        )

    # distinct names serialize fine (in-memory keys are the homes)
    reg_map = {
        "/home/x/.ava-t1": rec("/home/x/.ava-t1", "t1"),
        "/home/x/.ava-t2": rec("/home/x/.ava-t2", "t2"),
    }
    disk = cluster._registry_disk_form(reg_map)
    assert set(disk) == {"t1", "t2"}

    # same name, two homes -> refuse (a dict comprehension would last-win)
    import pytest

    reg_map = {
        "/home/x/.ava-t1": rec("/home/x/.ava-t1", "preview"),
        "/home/x/.ava-t2": rec("/home/x/.ava-t2", "preview"),
    }
    with pytest.raises(RuntimeError, match="share the compat name"):
        cluster._registry_disk_form(reg_map)


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
    assert ports["restarter"] == base + len("restarter")
    assert ports["ops"] == base + len("ops")
