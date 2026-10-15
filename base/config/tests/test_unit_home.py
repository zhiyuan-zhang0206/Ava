from __future__ import annotations

from pathlib import Path

import pytest

from base.config.base import unit_home


def test_unit_home_follows_ava_home(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AVA_HOME", "/srv/.ava_gateway")
    assert unit_home() == Path("/srv/.ava_gateway")


def test_unit_home_defaults_to_home_ava(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AVA_HOME", raising=False)
    assert unit_home() == Path.home() / ".ava"


def test_pidfile_fields_rooted_under_unit_home(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AVA_HOME", "/srv/.ava_gateway")
    from base.config import Settings

    s = Settings()
    root = Path("/srv/.ava_gateway")
    assert s.services.gateway_pidfile == root / "run" / "gateway.pid"
    assert s.services.memory_root == root / "memory"
    assert s.services.memory_search_data_dir == root / "memory-search"
