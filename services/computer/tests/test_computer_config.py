"""The computer-use composition root builds its slice from the flat registry fields."""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest

from base.config import get_field, settings
from services.computer import mcp_daemon


def test_the_slice_carries_the_live_value_of_every_field(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings.daemon, "computer_use_lease_s", 7.5)
    monkeypatch.setattr(settings.daemon, "computer_use_session_idle_s", 3.25)
    config = mcp_daemon.computer_use_config()
    for field in dataclasses.fields(config):
        flat: Any = get_field(field.name)
        assert getattr(config, field.name) == flat, field.name
    assert (config.computer_use_lease_s, config.computer_use_session_idle_s) == (7.5, 3.25)
