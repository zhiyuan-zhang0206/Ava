"""The cluster-instance stop legs report a Postgres escalation like the journaled legs.

`stop_cluster_instance` serves `ava stop --force` (via `_stop_data_plane`) and
`ava cluster instance stop`. Neither owns a stop journal, but both must still be
loud: stderr plus the `postgres_stop_escalated` event
(docs/decisions/2026-10-02-pg-stop-escalates-to-immediate.md).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from base.cluster import postgres as owned_postgres
from cli.commands.data_plane import cluster_instance as instance


def test_stop_cluster_instance_reports_an_escalation(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    escalation = owned_postgres.Escalation(
        detail="fast shutdown did not complete within 277s", killed=(4242,)
    )
    emitted: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def fake_emit(*args: object, **kwargs: object) -> None:
        emitted.append((args, kwargs))

    def fake_stop(*args: object, **kwargs: object) -> owned_postgres.Escalation:
        return escalation

    def fake_pg_data_dir() -> None:
        return None

    def fake_port() -> None:
        return None

    def fake_pgbouncer_stop() -> None:
        return None

    monkeypatch.setattr("base.telemetry.emit", fake_emit)
    monkeypatch.setattr(
        instance, "settings", SimpleNamespace(data_plane=SimpleNamespace(is_remote=False))
    )
    monkeypatch.setattr(instance, "_pg_data_dir", fake_pg_data_dir)
    monkeypatch.setattr(instance.ownership, "configured_redis_port", fake_port)
    monkeypatch.setattr(instance.owned_postgres, "stop", fake_stop)
    monkeypatch.setattr("cli.commands.data_plane.pgbouncer.stop_pgbouncer", fake_pgbouncer_stop)

    assert instance.stop_cluster_instance() == 0

    assert len(emitted) == 1
    (args, kwargs) = emitted[0]
    assert args[1] == "postgres_stop_escalated"
    assert kwargs["level"] == "error"
    assert "ended by an immediate shutdown" in capsys.readouterr().err
