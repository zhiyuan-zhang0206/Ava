"""`ava start`'s gateway data-plane dispatch brings up this cluster's own instance.

Every cluster owns its Postgres+Redis; `_ensure_gateway_data_plane` reads the
cluster's registry record and starts the per-cluster instance on the record's
pg/redis ports. A missing record (defensive) is a hard error, not a silent
fall-through onto some shared instance.
"""

from pathlib import Path

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
    ports, with the data-plane identity read from this cluster's own db_url
    (names-as-data)."""
    monkeypatch.setattr(cluster, "get_record", lambda _home: _rec())  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(settings.data_plane, "cluster_secret", "sek")
    monkeypatch.setattr(
        settings.data_plane, "db_url", "postgresql://ava_main:sek@127.0.0.1:5433/ava_main"
    )
    own_calls: list[dict[str, object]] = []
    monkeypatch.setattr(_ci, "ensure_cluster_instance", lambda **kw: own_calls.append(kw) or 0)  # pyright: ignore[reportUnknownArgumentType]

    assert _start._ensure_gateway_data_plane() == 0
    # pgbouncer_port is derived for this pre-pooler record (no 'pgbouncer' key) →
    # the default home's fixed legacy 6433 (record_pgbouncer_port). identity comes
    # from the db_url username, not any name derivation.
    assert own_calls == [
        {
            "pg_port": 5433,
            "redis_port": 6380,
            "cluster_secret": "sek",
            "db_admin_password": "sek",
            "redis_admin_password": "sek",
            "redis_password": "sek",
            "pgbouncer_port": 6433,
            "identity": "ava_main",
        }
    ]


def test_gateway_data_plane_no_record_is_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A not-yet-registered home (defensive) → a hard error, never a bring-up."""
    monkeypatch.setattr(cluster, "get_record", lambda _home: None)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(
        _ci,
        "ensure_cluster_instance",
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
