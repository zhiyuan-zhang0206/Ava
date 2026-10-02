"""The page-server composition root builds its slice from the flat registry fields."""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest

from base.config import get_field, settings
from services.page_server import daemon


def test_the_slice_carries_the_live_value_of_every_field(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings.daemon, "page_server_poll_interval_seconds", 9.5)
    config = daemon.page_server_config()
    for field in dataclasses.fields(config):
        flat: Any = get_field(field.name)
        assert getattr(config, field.name) == flat, field.name
    assert config.page_server_poll_interval_seconds == 9.5
