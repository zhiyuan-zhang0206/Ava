"""Test support: the telemetry staleness guard the gateway lifespan builds."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from gateway.lgtm.telemetry_staleness import TelemetryStaleness


def use_heartbeat_age(
    monkeypatch: pytest.MonkeyPatch, read_heartbeat_age: Callable[..., float | None]
) -> None:
    """Make the app lifespan build a guard that checks on every call and reads `read_heartbeat_age`."""

    def build() -> TelemetryStaleness:
        return TelemetryStaleness(check_interval_s=0, read_heartbeat_age=read_heartbeat_age)

    monkeypatch.setattr("gateway.app.TelemetryStaleness", build)
