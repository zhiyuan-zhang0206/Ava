"""ava.agents.list_machines maps the gateway rows to Machine dataclasses."""

from __future__ import annotations

from ava import agents


def test_list_machines_maps_rows():
    rows = [
        {"name": "test-host", "description": "voice IO + browser", "live": True},
        {"name": "runner-a", "description": None, "live": False},
    ]
    result = agents.machines_from_rows(rows)
    assert result == [
        agents.Machine(name="test-host", description="voice IO + browser", live=True),
        agents.Machine(name="runner-a", description=None, live=False),
    ]


def test_machine_str_is_compact():
    m = agents.Machine(name="test-host", description="voice IO + browser", live=True)
    s = str(m)
    assert "test-host" in s and "voice IO + browser" in s and "live" in s
