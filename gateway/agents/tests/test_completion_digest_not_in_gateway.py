"""The completion digest is a loop of the heartbeat service, not of the gateway."""

from __future__ import annotations

import importlib.util

import gateway.app


def test_the_gateway_no_longer_flushes_completion_digests() -> None:
    """The gateway module is gone and its lifespan starts no flusher for it."""
    assert importlib.util.find_spec("gateway.agents.completion_notice_flusher") is None
    assert "completion_notice_flusher" not in vars(gateway.app)
