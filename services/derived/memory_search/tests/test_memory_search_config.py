"""The memory-search composition root builds its slice from the flat registry fields."""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest

from base.config import get_field, settings
from services.derived.memory_search import daemon


def test_the_slice_carries_the_live_value_of_every_field(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings.services, "memory_search_port", 18765)
    monkeypatch.setattr(settings.services, "memory_search_max_batch_rows", 11)
    config = daemon.memory_search_config()
    for field in dataclasses.fields(config):
        flat: Any = get_field(field.name)
        assert getattr(config, field.name) == flat, field.name
    assert (config.memory_search_port, config.memory_search_max_batch_rows) == (18765, 11)
