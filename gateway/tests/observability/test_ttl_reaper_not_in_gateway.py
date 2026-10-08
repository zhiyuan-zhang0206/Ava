"""The TTL reaper is its own service, not a loop of the gateway."""

from __future__ import annotations

import importlib.util

import gateway.app


def test_the_gateway_owns_no_reaper() -> None:
    """The reaper is its own service: the gateway module is gone and its lifespan
    neither imports nor starts one, so no loop in the gateway can kill a shell."""
    assert importlib.util.find_spec("gateway.ttl_reaper") is None
    assert not hasattr(gateway.app, "ttl_reaper")
