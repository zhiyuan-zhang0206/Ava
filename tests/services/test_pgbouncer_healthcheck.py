"""Native pooler diagnostics retain evidence without gaining repair authority."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from services.ava_root_glue.diagnostic_probes import pgbouncer


@pytest.mark.parametrize(
    "loopback,public,expected",
    [(True, True, "alive"), (True, False, "down"), (False, False, "down")],
)
def test_pooler_protocol_requires_native_custody_and_both_listeners(
    monkeypatch, loopback, public, expected
):
    from cli.commands import _pgbouncer as pooler
    from services.ava_root_glue import diagnostic_probes
    from services.healthchecks import owned_service
    from shared.cluster import ownership

    owner = object()
    monkeypatch.setattr("shared.cluster.get_record", lambda _: object())
    monkeypatch.setattr("shared.cluster.record_pgbouncer_port", lambda _: 6432)
    monkeypatch.setattr("shared.cluster.db_identity", lambda: "owner")
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
    seen = []

    def inspect(captured, port, protocol):
        seen.append((captured, port))
        return protocol()

    monkeypatch.setattr(owned_service, "_owned_tcp", inspect)
    monkeypatch.setattr(pooler, "pgbouncer_listener_reachable", lambda *_: loopback)
    monkeypatch.setattr(pooler, "pgbouncer_public_listener_reachable", lambda *_: public)
    repair = Mock(side_effect=AssertionError("diagnostic must not repair"))
    monkeypatch.setattr(pooler, "ensure_pgbouncer", repair)
    assert pgbouncer().verdict.value == expected
    assert seen == [(owner, 6432)]
    repair.assert_not_called()


def test_unknown_pooler_registry_is_unavailable(monkeypatch):
    monkeypatch.setattr("shared.cluster.get_record", lambda _: None)
    assert pgbouncer().verdict.value == "unavailable"
