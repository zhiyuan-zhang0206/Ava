from __future__ import annotations

import importlib.util

import gateway.app


def test_the_gateway_owns_no_reaper() -> None:
    assert importlib.util.find_spec("gateway.ttl_reaper") is None
    assert not hasattr(gateway.app, "ttl_reaper")
