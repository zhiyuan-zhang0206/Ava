"""The Service-processes block lists only gateway-only daemons.

restarter (a per-host daemon) moved to the per-machine roster; labeler +
memory_indexer stay here as the gateway-only set."""

import pytest

import gateway.routers.status as status_mod


def test_services_status_is_gateway_only(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(status_mod, "_check_pidfile", lambda _p: (True, 4242))  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    svc = status_mod._get_services_status()
    names = {item.name for item in svc.items}
    assert names == {"labeler", "memory_indexer"}
    assert "restarter" not in names  # per-host now -> roster, not here
