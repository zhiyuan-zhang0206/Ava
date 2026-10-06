"""Native pooler diagnostics retain evidence without gaining repair authority.

The diagnostic reads the pooler through `base.cluster` only. It cannot start one: the start
(`ensure_pgbouncer`) lives in `cli`, and the `services must not import cli` contract in
`pyproject.toml` is what keeps a diagnostic from importing it.
"""

from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import Mock

import pytest

from base.cluster import ClusterPorts
from base.cluster.dataplane import pooler as base_pooler
from base.cluster.record import ClusterRecord
from services.supervision.ava_root_glue.diagnostic_probes import pgbouncer


@pytest.mark.parametrize(
    "loopback,public,expected",
    [(True, True, "alive"), (True, False, "down"), (False, False, "down")],
)
def test_pooler_protocol_requires_both_authenticated_listeners(
    monkeypatch: pytest.MonkeyPatch, loopback: bool, public: bool, expected: str
) -> None:
    from base.cluster import ownership
    from services.supervision.ava_root_glue import diagnostic_probes

    owner = Mock(side_effect=AssertionError("read-only probe must not read PID custody"))

    def _fake_get_record(_home: Path) -> ClusterRecord | None:
        return ClusterRecord(
            ports=cast("ClusterPorts", {"pgbouncer": 6432}), gateway_home="/x", created_at=""
        )

    admin = SimpleNamespace(password="pooler-admin-credential")  # noqa: S106 — test fixture

    monkeypatch.setattr("base.cluster.get_record", _fake_get_record)
    monkeypatch.setattr("base.cluster.authority.read_pooler_admin", Mock(return_value=admin))
    monkeypatch.setattr(ownership, "pooler", owner)
    monkeypatch.setattr(
        diagnostic_probes,
        "settings",
        SimpleNamespace(data_plane=SimpleNamespace(cluster_secret="")),
    )

    def _fake_listener_reachable(port: int, password: str) -> bool:
        assert (port, password) == (6432, admin.password)
        return loopback

    def _fake_public_listener_reachable(*_a: object) -> bool:
        return public

    monkeypatch.setattr(base_pooler, "pgbouncer_listener_reachable", _fake_listener_reachable)
    monkeypatch.setattr(
        base_pooler, "pgbouncer_public_listener_reachable", _fake_public_listener_reachable
    )
    assert pgbouncer().verdict.value == expected


def test_unknown_pooler_record_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fake_get_record(_home: Path) -> ClusterRecord | None:
        return None

    monkeypatch.setattr("base.cluster.get_record", _fake_get_record)
    assert pgbouncer().verdict.value == "unavailable"
