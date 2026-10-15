from __future__ import annotations

from collections.abc import Callable

import pytest

from gateway import app
from gateway.lgtm.telemetry_staleness import TelemetryStaleness


def use_heartbeat_age(
    monkeypatch: pytest.MonkeyPatch, read_heartbeat_age: Callable[..., float | None]
) -> None:

    def build() -> TelemetryStaleness:
        return TelemetryStaleness(check_interval_s=0, read_heartbeat_age=read_heartbeat_age)

    monkeypatch.setattr(app, "TelemetryStaleness", build)
