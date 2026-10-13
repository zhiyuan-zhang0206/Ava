"""The alert reconciliation and the registry gauge are events-maintenance loops, not gateway loops."""

from __future__ import annotations

import importlib.util

import gateway.app


def test_the_gateway_no_longer_reconciles_alerts() -> None:
    """The reconciliation is a loop of the events-maintenance service: the gateway
    module is gone and its lifespan starts nothing for it."""
    assert importlib.util.find_spec("gateway.alerts.reconciliation") is None
    assert not hasattr(gateway.app, "reconciliation")


def test_the_gateway_no_longer_samples_the_registry() -> None:
    """The gauge is a loop of the events-maintenance service: the gateway module is
    gone and its lifespan starts no flusher for it."""
    assert importlib.util.find_spec("gateway.agents.max_id_gauge") is None
    assert "max_id" not in vars(gateway.app)
