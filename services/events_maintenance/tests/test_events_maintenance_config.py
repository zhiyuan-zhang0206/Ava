"""The events-maintenance composition root builds its slice from the flat registry fields."""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest

from base.config import get_field, settings
from services.events_maintenance import daemon


def test_the_slice_carries_the_live_value_of_every_field(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings.daemon, "events_resolution_burst_threshold", 17)
    monkeypatch.setattr(settings.general, "timezone", "Asia/Shanghai")
    config = daemon.events_maintenance_config()
    for field in dataclasses.fields(config):
        flat: Any = get_field(field.name)
        assert getattr(config, field.name) == flat, field.name
    assert config.events_resolution_burst_threshold == 17
    assert config.timezone == "Asia/Shanghai"
