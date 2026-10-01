"""ava.agents.list_machines maps the gateway rows to Machine dataclasses."""

from __future__ import annotations

import pytest

from ava import agents


def test_list_machines_maps_rows(monkeypatch: pytest.MonkeyPatch):
    rows = [
        {"name": "test-host", "description": "voice IO + browser", "live": True},
        {"name": "wsl", "description": None, "live": False},
    ]
    monkeypatch.setattr(agents._client, "list_machines", lambda: rows)  # pyright: ignore[reportUnknownArgumentType]
    result = agents.list_machines()
    assert result == [
        agents.Machine(name="test-host", description="voice IO + browser", live=True),
        agents.Machine(name="wsl", description=None, live=False),
    ]


def test_machine_str_is_compact(monkeypatch: pytest.MonkeyPatch):
    m = agents.Machine(name="test-host", description="voice IO + browser", live=True)
    s = str(m)
    assert "test-host" in s and "voice IO + browser" in s and "live" in s
