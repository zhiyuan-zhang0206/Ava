"""The hierarchy worker's composition root builds its slice from the flat registry fields."""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest

from base.config import get_field, settings
from services.hierarchy_worker import roots


def test_the_slice_carries_the_live_value_of_every_field(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings.daemon, "hierarchy_tail_max_per_tick", 13)
    monkeypatch.setattr(settings.daemon, "hierarchy_regen_min_reuse_ratio", 0.125)
    monkeypatch.setattr(settings.lm, "hierarchy_model", "hierarchy-model-x")
    config = roots.hierarchy_worker_config()
    for field in dataclasses.fields(config):
        flat: Any = get_field(field.name)
        assert getattr(config, field.name) == flat, field.name
    assert config.hierarchy_tail_max_per_tick == 13
    assert config.hierarchy_regen_min_reuse_ratio == 0.125
    assert config.hierarchy_model == "hierarchy-model-x"
