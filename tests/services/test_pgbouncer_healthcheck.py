"""Native pooler diagnostics retain evidence without gaining repair authority."""

from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import Mock

import pytest

from services.ava_root_glue.diagnostic_probes import pgbouncer
from shared.cluster.registry import ClusterRecord
from shared.daemon_health import DaemonProbe


@pytest.mark.parametrize(
    "loopback,public,expected",
    [(True, True, "alive"), (True, False, "down"), (False, False, "down")],
)
def test_pooler_protocol_requires_native_custody_and_both_listeners(
    monkeypatch: pytest.MonkeyPatch, loopback: bool, public: bool, expected: str
) -> None:
    from cli.commands import _pgbouncer as pooler
    from services.ava_root_glue import diagnostic_probes
    from services.healthchecks import owned_service
    from shared.cluster import ownership

    owner = object()

    def _fake_get_record(_home: Path) -> ClusterRecord | None:
        return cast(ClusterRecord, object())

    def _fake_record_pgbouncer_port(_rec: ClusterRecord) -> int:
        return 6432

    def _fake_db_identity() -> str:
        return "owner"

    monkeypatch.setattr("shared.cluster.get_record", _fake_get_record)
    monkeypatch.setattr("shared.cluster.record_pgbouncer_port", _fake_record_pgbouncer_port)
    monkeypatch.setattr("shared.cluster.db_identity", _fake_db_identity)
    monkeypatch.setattr(ownership, "pooler", Mock(return_value=owner))
    monkeypatch.setattr(
        diagnostic_probes,
        "settings",
        SimpleNamespace(
            data_plane=SimpleNamespace(
                db_admin_password="",
                cluster_secret="",
            )
        ),
    )
    seen: list[tuple[object, int]] = []

    def inspect(
        captured: object, port: int, protocol: Callable[[], bool | DaemonProbe]
    ) -> bool | DaemonProbe:
        seen.append((captured, port))
        return protocol()

    def _fake_listener_reachable(*_a: object) -> bool:
        return loopback

    def _fake_public_listener_reachable(*_a: object) -> bool:
        return public

    monkeypatch.setattr(owned_service, "_owned_tcp", inspect)
    monkeypatch.setattr(pooler, "pgbouncer_listener_reachable", _fake_listener_reachable)
    monkeypatch.setattr(
        pooler, "pgbouncer_public_listener_reachable", _fake_public_listener_reachable
    )
    repair = Mock(side_effect=AssertionError("diagnostic must not repair"))
    monkeypatch.setattr(pooler, "ensure_pgbouncer", repair)
    assert pgbouncer().verdict.value == expected
    assert seen == [(owner, 6432)]
    repair.assert_not_called()


def test_unknown_pooler_registry_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fake_get_record(_home: Path) -> ClusterRecord | None:
        return None

    monkeypatch.setattr("shared.cluster.get_record", _fake_get_record)
    assert pgbouncer().verdict.value == "unavailable"
