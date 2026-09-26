"""`ava start`'s gateway data-plane dispatch brings up this cluster's own instance.

Every cluster owns its Postgres+Redis; `_ensure_gateway_data_plane` reads the
cluster's registry record and starts the per-cluster instance on the record's
pg/redis ports. A missing record (defensive) is a hard error, not a silent
fall-through onto some shared instance.
"""

from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from cli.commands import _cluster_instance as _ci
from cli.commands import start as _start
from cli.commands._converge_spec import ConvergeCtx
from shared import cluster
from shared.config import settings

_PORTS: cluster.ClusterPorts = {
    "gateway": 8000,
    "frontend": 3000,
    "restarter": 8102,  # Reserved registry slot in existing home records.
    "labeler": 8103,
    "memory_indexer": 8105,
    "ops": 8106,
    "milvus": 19530,
    "browser": 9222,
    "permissions_helper": 9223,
    "postgres": 5433,
    "redis": 6380,
}


def _rec() -> cluster.ClusterRecord:
    return cluster.ClusterRecord(
        ports=_PORTS,
        gateway_home=str(cluster.default_home()),
        created_at="x",
    )


def test_gateway_data_plane_brings_up_own_instance(monkeypatch: pytest.MonkeyPatch) -> None:
    """A born cluster → the per-cluster instance on the record's exact pg/redis
    ports, with each data-plane identity read from its own URL."""
    monkeypatch.setattr(cluster, "get_record", lambda _home: _rec())  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(settings.data_plane, "cluster_secret", "bearer")
    monkeypatch.setattr(settings.data_plane, "db_admin_password", "owner")
    monkeypatch.setattr(settings.data_plane, "redis_admin_password", "redis-admin")
    monkeypatch.setattr(cluster, "redis_password_from_env", lambda: "redis-runtime")
    monkeypatch.setattr(
        settings.data_plane, "db_url", "postgresql://ava_main:sek@127.0.0.1:5433/ava_main"
    )
    monkeypatch.setattr(settings.data_plane, "redis_url", "redis://ava:sek@127.0.0.1:6380/0")
    own_calls: list[dict[str, object]] = []
    monkeypatch.setattr(_ci, "ensure_cluster_storage", lambda **kw: own_calls.append(kw) or 0)  # pyright: ignore[reportUnknownArgumentType]

    assert _start._ensure_gateway_data_plane() == 0
    # pgbouncer_port is derived for this pre-pooler record (no 'pgbouncer' key) →
    # the default home's fixed legacy 6433 (record_pgbouncer_port). identity comes
    # from the db_url username, not any name derivation.
    assert own_calls == [
        {
            "pg_port": 5433,
            "redis_port": 6380,
            "cluster_secret": "bearer",
            "db_admin_password": "owner",
            "redis_admin_password": "redis-admin",
            "redis_password": "redis-runtime",
            "redis_user": "ava",
        }
    ]


def test_gateway_data_plane_no_record_is_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A not-yet-registered home (defensive) → a hard error, never a bring-up."""
    monkeypatch.setattr(cluster, "get_record", lambda _home: None)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(
        _ci,
        "ensure_cluster_storage",
        lambda **_kw: pytest.fail("bring-up without a record"),  # pyright: ignore[reportUnknownArgumentType]
    )

    assert _start._ensure_gateway_data_plane() == 1


# ─── port preflight (warning-only, runs in converge before launch) ──────────


def _preflight_ctx(tmp_path: Path) -> ConvergeCtx:
    return ConvergeCtx(
        repo=tmp_path / "repo",
        ava_home=tmp_path / "home",
        roles=frozenset({"gateway"}),
    )


def test_port_preflight_warns_and_logs_conflicts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
):
    """A foreign occupant on the cluster block → the start CONTINUES (rc-free
    step) but prints the warning and appends it to $AVA_HOME/logs/port_conflicts.log."""
    from cli.commands import _port_preflight as _pp
    from shared import cluster as _cluster

    ctx = _preflight_ctx(tmp_path)
    monkeypatch.setattr(
        _pp,
        "collect_port_conflicts",
        lambda _ctx: ["gateway 8000 listener(s): pid 1 (nginx)"],  # pyright: ignore[reportUnknownArgumentType]
    )
    monkeypatch.setattr(_cluster, "get_record", lambda _home: None)  # pyright: ignore[reportUnknownArgumentType]

    _pp.ensure_port_preflight(ctx)  # must not raise — a preflight never fails a start

    err = capsys.readouterr().err  # pyright: ignore[reportUnknownMemberType]
    assert "PORT PREFLIGHT" in err and "gateway 8000" in err
    log = tmp_path / "home" / "logs" / "port_conflicts.log"
    assert log.exists()
    lines = log.read_text().splitlines()
    assert len(lines) == 1 and lines[0].endswith("gateway 8000 listener(s): pid 1 (nginx)")


def test_port_preflight_silent_when_clean(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys):
    """No conflicts and no drift → no output, no log file."""
    from cli.commands import _port_preflight as _pp
    from shared import cluster as _cluster

    ctx = _preflight_ctx(tmp_path)
    monkeypatch.setattr(_pp, "collect_port_conflicts", lambda _ctx: [])  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_cluster, "get_record", lambda _home: None)  # pyright: ignore[reportUnknownArgumentType]

    _pp.ensure_port_preflight(ctx)

    assert capsys.readouterr().err == ""  # pyright: ignore[reportUnknownMemberType]
    assert not (tmp_path / "home" / "logs" / "port_conflicts.log").exists()


def test_port_preflight_never_fails_start_on_scan_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
):
    """A scan exception prints a notice and returns — the step must not turn a
    warning pass into a failed start."""
    from cli.commands import _port_preflight as _pp

    ctx = _preflight_ctx(tmp_path)
    monkeypatch.setattr(
        _pp,
        "collect_port_conflicts",
        lambda _ctx: (_ for _ in ()).throw(RuntimeError("boom")),  # pyright: ignore[reportUnknownArgumentType]
    )

    _pp.ensure_port_preflight(ctx)

    assert "port preflight skipped: boom" in capsys.readouterr().err  # pyright: ignore[reportUnknownMemberType]


def test_collect_port_conflicts_detects_foreign_listener(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """A bound socket whose process is not this unit (a pytest process) reads as
    a conflict; once released, the same port is clean.

    The whole scan is pinned to the socket's port: `unit_port_map` also overlays
    this unit's health ports, each pinned to a port that was free at session
    start — on a shared runner a CONCURRENT job can bind one of those in the
    meantime, and its daemon then reads as a foreign occupant (extra conflict
    lines, the 2026-08-02 CI flake, run 30736794423). What is under test is the
    ownership rule, not the map, so the map is stubbed to one port."""
    import socket

    from cli.commands import _port_preflight as _pp

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    port = sock.getsockname()[1]

    ctx = _preflight_ctx(tmp_path)
    monkeypatch.setattr(_pp, "unit_port_map", lambda _home: {"gateway": port})  # pyright: ignore[reportUnknownArgumentType]
    try:
        lines = _pp.collect_port_conflicts(ctx)
        assert len(lines) == 1 and lines[0].startswith("gateway")
        sock.close()
        # released → the port reads clean again
        assert _pp.collect_port_conflicts(ctx) == []
    finally:
        sock.close()


def test_collect_port_conflicts_env_layer_overrides_block_for_enrolled_unit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """F-s4-2 companion: a record-less unit enrolled with `--health-port-base`
    has its REAL per-unit ports in `.env` — the preflight must check THOSE, not
    the legacy segment the block layer would otherwise claim. Before the S4
    fix the `.env` layer only filled gaps, so an enrolled unit's health ports
    were never checked (the exact gap im_bridge/delivery_watchdog fell into)."""
    from cli.commands import _port_preflight as _pp

    ctx = _preflight_ctx(tmp_path)
    # no registry record -> block layer is the legacy segment
    monkeypatch.setattr(
        "shared.port_preflight.expected_cluster_ports",
        lambda _home: {"agent_host": 8121},  # pyright: ignore[reportUnknownArgumentType]
    )
    # the unit's own .env declares a per-unit block port (every health daemon
    # resolves; only agent_host's matters for the assertion)
    per_unit = {"agent_host": 20003}
    monkeypatch.setattr(
        "shared.daemon_health.health_port",
        lambda svc: per_unit.get(svc, 20000 + len(svc)),  # pyright: ignore[reportUnknownArgumentType]
    )

    # occupy the .env port: it must be reported despite the block layer naming
    # the legacy 8121
    import socket

    sock = socket.socket()
    sock.bind(("127.0.0.1", 20003))
    sock.listen(1)
    try:
        lines = _pp.collect_port_conflicts(ctx)
        assert any("agent_host" in ln and "20003" in ln for ln in lines), lines
    finally:
        sock.close()


@pytest.fixture
def completing_plane(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    from cli.commands import _data_plane, _health_preflight

    calls: list[str] = []
    monkeypatch.setattr(
        _data_plane, "prepare_memory_vectors", lambda: calls.append("memory-vectors")
    )
    monkeypatch.setattr(cluster, "get_record", lambda _home: _rec())  # pyright: ignore[reportUnknownArgumentType] — test double or third-party stubs
    monkeypatch.setattr(settings.data_plane, "db_url", "postgresql://ava@127.0.0.1:6433/ava")
    monkeypatch.setattr(settings.data_plane, "redis_url", "redis://127.0.0.1:6380/0")
    monkeypatch.setattr(settings.data_plane, "pgbouncer_enabled", True)
    monkeypatch.setattr(
        cluster,
        "ensure_pgvector_extension",
        lambda *_a, **_kw: calls.append("extension"),  # pyright: ignore[reportUnknownArgumentType] — test double or third-party stubs
    )
    monkeypatch.setattr(cluster, "ensure_runner_role", lambda *_a, **_kw: calls.append("grant"))  # pyright: ignore[reportUnknownArgumentType] — test double or third-party stubs
    monkeypatch.setattr(cluster, "runner_password_from_env", lambda: "runner-password")
    monkeypatch.setattr(_ci, "_start_pgbouncer", lambda **_kw: calls.append("pooler") or 0)  # pyright: ignore[reportUnknownArgumentType] — test double or third-party stubs
    monkeypatch.setattr(
        _health_preflight,
        "probe_postgres",
        lambda url: calls.append("runner-probe" if "ava_runner" in url else "owner-probe"),  # pyright: ignore[reportUnknownArgumentType] — test double or third-party stubs
    )
    monkeypatch.setattr(_health_preflight, "probe_redis", lambda _url: calls.append("redis-probe"))  # pyright: ignore[reportUnknownArgumentType] — test double or third-party stubs
    monkeypatch.setattr("cli.start_identity.mark_phase", lambda *_a: calls.append("provisioned"))  # pyright: ignore[reportUnknownArgumentType] — test double or third-party stubs
    return calls


def test_grants_precede_first_pooler_login_and_consumer_probes(completing_plane: list[str]) -> None:
    from cli.commands._data_plane import complete_gateway_data_plane

    complete_gateway_data_plane()
    assert completing_plane == [
        "extension",
        "memory-vectors",
        "grant",
        "pooler",
        "owner-probe",
        "runner-probe",
        "redis-probe",
        "provisioned",
    ]


def test_release_readiness_performs_no_schema_or_grant_writes(completing_plane: list[str]) -> None:
    from cli.commands._data_plane import complete_gateway_data_plane

    complete_gateway_data_plane(refresh_schema=False)
    assert completing_plane == [
        "pooler",
        "owner-probe",
        "runner-probe",
        "redis-probe",
        "provisioned",
    ]


def test_failed_grants_never_start_pooler_or_mark_provisioned(
    completing_plane: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from cli.commands._data_plane import complete_gateway_data_plane

    def fail(*_a: object, **_kw: object) -> None:
        raise RuntimeError("grant refused")

    monkeypatch.setattr(cluster, "ensure_runner_role", fail)
    with pytest.raises(RuntimeError, match="grant refused"):
        complete_gateway_data_plane()
    assert completing_plane == ["extension", "memory-vectors"]


def test_pooler_disabled_still_requires_owner_runner_and_redis_readiness(
    completing_plane: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from cli.commands._data_plane import complete_gateway_data_plane

    monkeypatch.setattr(settings.data_plane, "pgbouncer_enabled", False)
    complete_gateway_data_plane()
    assert completing_plane == [
        "extension",
        "memory-vectors",
        "grant",
        "owner-probe",
        "runner-probe",
        "redis-probe",
        "provisioned",
    ]


class _Authority:
    """Records the owner-authority dial `prepare_memory_vectors` must use."""

    def __init__(self, calls: list[str]) -> None:
        self._calls = calls

    @contextmanager
    def session(self) -> Generator[str]:
        self._calls.append("owner-session")
        yield "owner-connection"


def test_memory_vectors_prepared_as_owner_only_for_pgvector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The pgvector table is start-time DDL through the owner authority at the
    provider's dimension; any other backend dials nothing."""
    from cli.commands._data_plane import prepare_memory_vectors
    from services.memory_indexer.backends import pgvector
    from services.memory_indexer.embeddings import factory
    from shared import pg_admin

    calls: list[str] = []
    monkeypatch.setattr(pg_admin, "local_owner_authority", lambda: _Authority(calls))

    def prepare(conn: str, dim: int) -> None:
        calls.append(f"prepare:{conn}:{dim}")

    monkeypatch.setattr(pgvector, "prepare_table", prepare)
    monkeypatch.setattr(factory, "get_provider", lambda: SimpleNamespace(dim=3072))

    monkeypatch.setattr(settings.services, "memory_search_backend", "numpy")
    prepare_memory_vectors()
    assert calls == []

    monkeypatch.setattr(settings.services, "memory_search_backend", "pgvector")
    prepare_memory_vectors()
    assert calls == ["owner-session", "prepare:owner-connection:3072"]


def test_remote_plane_prepares_memory_vectors_through_its_provider_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A remote-managed plane has no local owner authority; its provider URL
    carries the table DDL, exactly as it carries the plane's migrations."""
    import shared.db
    from cli.commands._data_plane import prepare_memory_vectors
    from services.memory_indexer.backends import pgvector
    from services.memory_indexer.embeddings import factory
    from shared import pg_admin

    calls: list[str] = []

    @contextmanager
    def provider(**kwargs: object) -> Generator[str]:
        calls.append(f"provider:{kwargs}")
        yield "provider-connection"

    def prepare(conn: str, dim: int) -> None:
        calls.append(f"prepare:{conn}:{dim}")

    monkeypatch.setattr(settings.data_plane, "db_url", "postgresql://owner@db.example/ava")
    monkeypatch.setattr(settings.services, "memory_search_backend", "pgvector")
    monkeypatch.setattr(shared.db, "connect", provider)
    monkeypatch.setattr(pg_admin, "local_owner_authority", lambda: pytest.fail("no local admin"))
    monkeypatch.setattr(pgvector, "prepare_table", prepare)
    monkeypatch.setattr(factory, "get_provider", lambda: SimpleNamespace(dim=768))

    prepare_memory_vectors()

    assert calls == ["provider:{'direct': True}", "prepare:provider-connection:768"]


@pytest.mark.parametrize(
    ("secret", "missing", "reason"),
    [
        ("bearer-only", "db_admin_password", "explicit owner"),
        ("bearer-only", "redis_admin_password", "cutover_db_authority"),
        ("bearer-only", "redis_password", "cutover_db_authority"),
        ("", "redis_admin_password", "cutover_db_authority"),
        ("", "redis_password", "cutover_db_authority"),
    ],
)
def test_storage_refuses_missing_credentials_before_effects(
    monkeypatch: pytest.MonkeyPatch, secret: str, missing: str, reason: str
) -> None:
    """Redis always authenticates, so an empty bearer does not excuse missing
    Redis credentials: an unconverted home is refused (naming the one-time
    cutover) before any native effect. Only the Postgres owner credential still
    follows the bearer."""
    credentials = {
        "db_admin_password": "owner-value",
        "redis_admin_password": "admin-value",
        "redis_password": "runtime-value",
    }
    credentials[missing] = ""
    monkeypatch.setattr(
        _ci,
        "_start_pg",
        lambda *_a: pytest.fail("storage started before validation"),  # pyright: ignore[reportUnknownArgumentType] — test double or third-party stubs
    )
    with pytest.raises(ValueError, match=reason):
        _ci.ensure_cluster_storage(
            pg_port=5433,
            redis_port=6380,
            cluster_secret=secret,
            redis_user="ava",
            **credentials,
        )


def test_authenticated_pooler_does_not_substitute_bearer(monkeypatch: pytest.MonkeyPatch) -> None:
    from cli.commands import _pgbouncer

    monkeypatch.setattr(
        _pgbouncer, "pgbouncer_bin", lambda: pytest.fail("pooler prepared before validation")
    )
    with pytest.raises(ValueError, match="explicit database owner"):
        _pgbouncer.ensure_pgbouncer(
            pg_port=5433,
            listen_port=6433,
            db_name="ava",
            role="ava",
            cluster_secret="bearer-only",  # noqa: S106 — isolated test credential
            db_admin_password="",
            runner_password="runner-value",  # noqa: S106 — isolated test credential
        )
