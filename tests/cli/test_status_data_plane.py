"""`ava status` data-plane rendering (`print_data_plane_status`).

The pgbouncer line must show the port the pooler actually LISTENS on — the
registry-derived `record_pgbouncer_port(rec)`, the same value
`ensure_cluster_instance` starts it with. The pooler port is a registry fact
only (AVA_PGBOUNCER_PORT is no longer materialized in `.env` — AVA_DB_URL
carries the pooler port when pooling is on), so no `.env` cache can go stale;
without a registry record the line says the port is unresolvable instead of
printing a false `:0` (the 2026-07-20 symptom class).
"""

from __future__ import annotations

from typing import cast

import pytest

import cli.commands._cluster_instance as ci
import cli.commands._pgbouncer as pgb
import shared.cluster as cl
from shared.config import settings


def test_pgbouncer_line_uses_registry_port(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The pooler port is a registry fact only: status derives 6433 from the
    registry (the same value ensure_cluster_instance starts it with) and probes
    that exact port — there is no .env copy that could go stale."""
    monkeypatch.setattr(
        ci,
        "_pg_running",
        lambda _p: False,  # pyright: ignore[reportUnknownArgumentType]
    )  # skip the real pg probe/connect  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(
        ci,
        "_redis_reachable",
        lambda _p: False,  # pyright: ignore[reportUnknownArgumentType]
    )  # skip the real redis probe  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(settings.data_plane, "pgbouncer_enabled", True)

    fake_rec = cl.ClusterRecord(ports=cast("cl.ClusterPorts", {}), gateway_home="/x", created_at="")
    monkeypatch.setattr(cl, "get_record", lambda _home: fake_rec)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(cl, "record_pgbouncer_port", lambda _rec: 6433)  # pyright: ignore[reportUnknownArgumentType]

    probed: dict[str, int] = {}

    def _reachable(port, *_a):  # type: ignore[no-untyped-def]
        probed["port"] = port
        return True

    monkeypatch.setattr(pgb, "pgbouncer_reachable", _reachable)  # pyright: ignore[reportUnknownArgumentType]

    ci.print_data_plane_status()
    out = capsys.readouterr().out
    assert "pgbouncer (127.0.0.1:6433" in out
    assert "127.0.0.1:0" not in out  # never the stale-settings zero
    assert probed["port"] == 6433  # the reachability probe hit the real listen port


def test_pgbouncer_line_without_registry_record_says_so(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """No registry record (an unusual host): the pooler port is a registry fact,
    so status says it cannot resolve the port instead of printing a false :0."""
    monkeypatch.setattr(ci, "_pg_running", lambda _p: False)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(ci, "_redis_reachable", lambda _p: False)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(settings.data_plane, "pgbouncer_enabled", True)
    monkeypatch.setattr(cl, "get_record", lambda _home: None)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(pgb, "pgbouncer_reachable", lambda *_a: True)  # pyright: ignore[reportUnknownArgumentType]

    ci.print_data_plane_status()
    out = capsys.readouterr().out
    assert "no registry record" in out
    assert "127.0.0.1:0" not in out


def test_postgres_probe_dials_pooled_front_door(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The postgres auth probe uses `connect()` — the POOLED front door (PgBouncer
    when enabled, the direct URL when not) — never `connect(direct=True)`.

    F8a (user ruling 2026-08 "always PgBouncer"): the pooled SELECT 1 proves the
    path every consumer dials (client scram at the pooler + the trust-socket
    backend hop); a direct probe would test a path no consumer uses."""
    import shared.db

    monkeypatch.setattr(ci, "_pg_running", lambda _p: True)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(ci, "_redis_reachable", lambda _p: False)  # pyright: ignore[reportUnknownArgumentType]

    calls: list[dict[str, object]] = []

    class _FakeConn:
        def __enter__(self) -> _FakeConn:
            return self

        def __exit__(self, *exc: object) -> None:
            return None

        def execute(self, _sql: str) -> None:
            return None

    def _fake_connect(**kwargs: object) -> _FakeConn:
        calls.append(kwargs)
        return _FakeConn()

    monkeypatch.setattr(shared.db, "connect", _fake_connect)
    # pgbouncer off: pooled_db_url == db_url, the probe is direct in effect.
    monkeypatch.setattr(settings.data_plane, "pgbouncer_enabled", False)
    ci.print_data_plane_status()
    assert calls and "direct" not in calls[0]  # never direct=True

    # pgbouncer on: the probe still dials the pooled URL, never direct.
    calls.clear()
    monkeypatch.setattr(settings.data_plane, "pgbouncer_enabled", True)
    monkeypatch.setattr(pgb, "pgbouncer_reachable", lambda *_a: True)  # pyright: ignore[reportUnknownArgumentType]
    ci.print_data_plane_status()
    out = capsys.readouterr().out
    assert calls and "direct" not in calls[0]
    assert "✓ postgres" in out
