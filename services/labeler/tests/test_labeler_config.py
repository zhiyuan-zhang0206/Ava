"""The labeler's composition root builds its slice from the flat registry fields."""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

import pytest

from base.config import get_field, settings
from base.daemon.endpoints import ServiceEndpoint
from services.labeler import daemon


def test_the_slice_carries_the_live_value_of_every_field(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings.lm, "labeler_model", "label-model-x")
    monkeypatch.setattr(settings.services, "labeler_max_chars", 33)
    config = daemon.labeler_config()
    for field in dataclasses.fields(config):
        flat: Any = get_field(field.name)
        assert getattr(config, field.name) == flat
    assert (config.labeler_model, config.labeler_max_chars) == ("label-model-x", 33)


def test_run_hands_the_slice_to_the_dispatch_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    received: list[object] = []

    class _Health:
        pass

    async def fake_start(_name: str, _port: int, **_kwargs: Any) -> _Health:
        return _Health()

    async def fake_stop(_server: object) -> None:
        pass

    class _Pool:
        def close(self) -> None:
            pass

    async def fake_dispatch(_pool: object, _db: object, _liveness: object, config: object) -> None:
        received.append(config)

    monkeypatch.setattr(daemon, "_is_running", lambda: False)
    monkeypatch.setattr(daemon, "_write_pidfile", lambda: None)
    monkeypatch.setattr(daemon, "_remove_pidfile", lambda: None)
    monkeypatch.setattr(daemon, "start_health_server", fake_start)
    monkeypatch.setattr(daemon, "stop_health_server", fake_stop)

    def fake_pool(_self: object) -> _Pool:
        return _Pool()

    monkeypatch.setattr(
        daemon, "_endpoint", lambda: ServiceEndpoint("labeler", 1, Path("/nonexistent/labeler.pid"))
    )
    monkeypatch.setattr(daemon.Database, "pool", fake_pool)
    monkeypatch.setattr(daemon, "_dispatch_loop", fake_dispatch)

    asyncio.run(daemon.run())
    assert received == [daemon.labeler_config()]
