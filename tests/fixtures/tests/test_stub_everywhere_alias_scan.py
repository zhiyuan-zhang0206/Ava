from __future__ import annotations

import sys
import types

import pytest

from base.db import Database
from base.events.live.bus import EventBus
from tests.fixtures.guards import stub_everywhere


def _stub(_db: Database, _bus: EventBus, *args: object, **kwargs: object) -> None:
    del args, kwargs


def test_alias_scan_does_not_probe_module_getattr(monkeypatch: pytest.MonkeyPatch) -> None:
    import ops.cluster_status.pause

    touched: list[str] = []

    def _dynamic(name: str) -> None:
        touched.append(name)
        raise AttributeError(name)

    dynamic = types.ModuleType("stub_everywhere_alias_scan_probe")
    dynamic.__getattr__ = _dynamic
    monkeypatch.setitem(sys.modules, dynamic.__name__, dynamic)

    stub_everywhere(monkeypatch, ops.cluster_status.pause, "unpause_local_cluster", _stub)

    assert touched == []


def test_alias_scan_does_not_trigger_the_mcps_servers_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import meiva.mcps
    import ops.cluster_status.pause

    calls: list[str] = []
    real_servers = meiva.mcps.servers

    def _spy() -> list[str]:
        calls.append("servers")
        return real_servers()

    monkeypatch.setattr(meiva.mcps, "servers", _spy)
    stub_everywhere(monkeypatch, ops.cluster_status.pause, "unpause_local_cluster", _stub)

    assert calls == []


def test_alias_scan_still_rebinds_a_frozen_alias(monkeypatch: pytest.MonkeyPatch) -> None:
    import ops.cluster_status.pause

    real = ops.cluster_status.pause.unpause_local_cluster
    holder = types.ModuleType("stub_everywhere_alias_scan_holder")
    holder.__dict__["unpause_local_cluster"] = real
    monkeypatch.setitem(sys.modules, holder.__name__, holder)

    stub_everywhere(monkeypatch, ops.cluster_status.pause, "unpause_local_cluster", _stub)

    assert holder.__dict__["unpause_local_cluster"] is _stub
