"""`ava start`'s gateway data-plane dispatch brings up this cluster's own instance.

Every cluster owns its Postgres+Redis; `_ensure_gateway_data_plane` reads the
cluster's registry record and starts the per-cluster instance on the record's
pg/redis ports. A missing record (defensive) is a hard error, not a silent
fall-through onto some shared instance.
"""

import pytest

from base import cluster
from base.config import settings
from cli.commands.data_plane import cluster_instance as _ci
from cli.commands.lifecycle import start as _start
from tests.factories.data_plane import cluster_record


def _rec() -> cluster.ClusterRecord:
    return cluster_record({"gateway": 23000, "postgres": 23011, "redis": 23012}, created_at="x")


def test_gateway_data_plane_brings_up_own_instance(monkeypatch: pytest.MonkeyPatch) -> None:
    """A born cluster → the per-cluster instance on the record's exact pg/redis
    ports, with each data-plane identity read from its own URL."""
    monkeypatch.setattr(cluster, "get_record", lambda _home: _rec())  # pyright: ignore[reportUnknownArgumentType]
    # An established home: its database authority ledger exists.
    monkeypatch.setattr("base.cluster.authority.load_ledger", lambda _home: object())  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(settings.data_plane, "cluster_secret", "bearer")
    monkeypatch.setattr(settings.data_plane, "redis_admin_password", "redis-admin")
    monkeypatch.setattr(cluster, "redis_password_from_env", lambda: "redis-runtime")
    monkeypatch.setattr(
        settings.data_plane, "db_url", "postgresql://ava_main:sek@127.0.0.1:23011/ava_main"
    )
    monkeypatch.setattr(settings.data_plane, "redis_url", "redis://ava:sek@127.0.0.1:23012/0")
    own_calls: list[dict[str, object]] = []
    monkeypatch.setattr(_ci, "ensure_cluster_storage", lambda **kw: own_calls.append(kw) or 0)  # pyright: ignore[reportUnknownArgumentType]

    assert _start._ensure_gateway_data_plane(retained_children=[]) == 0
    # The root that holds the secret published the telemetry token the station probe reads.
    from base.cluster.authority.api import read_telemetry_token, telemetry_token
    from base.paths import ava_home

    assert read_telemetry_token(ava_home().resolve()) == telemetry_token("bearer")
    # The identity comes from the db_url username, not any name derivation.
    assert own_calls == [
        {
            "pg_port": 23011,
            "redis_port": 23012,
            "cluster_secret": "bearer",
            "redis_admin_password": "redis-admin",
            "redis_password": "redis-runtime",
            "redis_user": "ava",
            "retained_children": [],
        }
    ]


def test_gateway_data_plane_refuses_a_home_without_a_ledger_before_any_effect(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A home born before the always-authenticated data plane (no ledger, not a
    first start in progress) is refused before any native effect — never
    converted by an ordinary start."""
    monkeypatch.setattr(cluster, "get_record", lambda _home: _rec())  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr("base.cluster.authority.load_ledger", lambda _home: None)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr("cli.start_identity.needs_provision", lambda _home: False)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(
        _ci,
        "ensure_cluster_storage",
        lambda **_kw: pytest.fail("native effect on a legacy home"),  # pyright: ignore[reportUnknownArgumentType]
    )
    assert _start._ensure_gateway_data_plane(retained_children=[]) == 1
    assert "no conversion exists" in capsys.readouterr().err


def test_gateway_data_plane_no_record_is_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A not-yet-registered home (defensive) → a hard error, never a bring-up."""
    monkeypatch.setattr(cluster, "get_record", lambda _home: None)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(
        _ci,
        "ensure_cluster_storage",
        lambda **_kw: pytest.fail("bring-up without a record"),  # pyright: ignore[reportUnknownArgumentType]
    )

    assert _start._ensure_gateway_data_plane(retained_children=[]) == 1


def test_local_launch_without_owner_cannot_publish_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import Mock

    from cli.commands.data_plane import bringup

    effect = Mock(side_effect=AssertionError("authority must not be published"))
    monkeypatch.setattr("base.cluster.authority.api.publish_telemetry_token", effect)
    with pytest.raises(ValueError, match="caller-owned child retention"):
        bringup.ensure_gateway_data_plane()
    effect.assert_not_called()
