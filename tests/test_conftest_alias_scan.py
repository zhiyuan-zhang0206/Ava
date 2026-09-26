"""`tests/conftest.py::_stub_everywhere`'s alias scan is static by contract.

The helper rebinds every already-imported frozen alias of a guarded function
(the spawn/reap entry points). The scan that finds those aliases must read
module `__dict__`s only: probing `getattr(mod, name)` invokes module-level
`__getattr__` (PEP 562), and a dynamic surface may run arbitrary code during
the probe — `ava.mcps.__getattr__` formats its "no such server" message by
calling the metered `servers()`, so the old probe emitted a burst of
`sdk_call` telemetry rows on every test setup. Those rows landed in the
per-test event mirror and made `tests/gateway/test_log_sink.py` fail when run
standalone (task #3950). A dynamically-served name is not a frozen alias
anyway: the real object was never bound into that module's dict, which is
exactly the surface the rebind sets on.

Locked here: neither a real PEP 562 module nor the repo's own dynamic
`ava.mcps` surface is touched by the scan, and a genuine frozen alias is
still rebound.
"""

from __future__ import annotations

import sys
import types

import pytest

from tests.conftest import _stub_everywhere


def _stub(*args: object, **kwargs: object) -> None:
    del args, kwargs


def test_alias_scan_does_not_probe_module_getattr(monkeypatch: pytest.MonkeyPatch) -> None:
    """A PEP 562 module `__getattr__` must not run during the alias scan."""
    import ops.cluster_pause

    touched: list[str] = []

    def _dynamic(name: str) -> None:
        touched.append(name)
        raise AttributeError(name)

    dynamic = types.ModuleType("_conftest_alias_scan_probe")
    dynamic.__getattr__ = _dynamic
    monkeypatch.setitem(sys.modules, dynamic.__name__, dynamic)

    _stub_everywhere(monkeypatch, ops.cluster_pause, "unpause_local_cluster", _stub)

    assert touched == []


def test_alias_scan_does_not_trigger_the_mcps_servers_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The scan must not reach `ava.mcps.servers()` through the module's
    `__getattr__` — every unit of the #3950 telemetry burst came from this
    probe."""
    import ava.mcps
    import ops.cluster_pause

    calls: list[str] = []
    real_servers = ava.mcps.servers

    def _spy() -> list[str]:
        calls.append("servers")
        return real_servers()

    monkeypatch.setattr(ava.mcps, "servers", _spy)
    _stub_everywhere(monkeypatch, ops.cluster_pause, "unpause_local_cluster", _stub)

    assert calls == []


def test_alias_scan_still_rebinds_a_frozen_alias(monkeypatch: pytest.MonkeyPatch) -> None:
    """The other half of the contract: a module that holds the real function
    object is still found and rebound — the scan's whole purpose."""
    import ops.cluster_pause

    real = ops.cluster_pause.unpause_local_cluster
    holder = types.ModuleType("_conftest_alias_scan_holder")
    holder.__dict__["unpause_local_cluster"] = real
    monkeypatch.setitem(sys.modules, holder.__name__, holder)

    _stub_everywhere(monkeypatch, ops.cluster_pause, "unpause_local_cluster", _stub)

    assert holder.__dict__["unpause_local_cluster"] is _stub
