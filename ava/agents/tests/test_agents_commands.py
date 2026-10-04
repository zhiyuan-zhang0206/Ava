"""`ava.agents.commands()` lists a peer's commands without their bodies."""

import pytest

from ava.skills import composer_commands


def test_agents_commands_lists_name_desc_hint_no_body(monkeypatch: pytest.MonkeyPatch):
    from ava import agents

    monkeypatch.setattr(
        composer_commands,
        "discover_commands",
        lambda: [
            {
                "name": "recap",
                "description": "recap it",
                "instruction_hint": "a focus",
                "body": "SECRET BODY",
                "skill_target": None,
            }
        ],
    )
    out = agents.commands()
    assert len(out) == 1
    info = out[0]
    assert (info.name, info.description, info.instruction_hint) == ("recap", "recap it", "a focus")
    assert not hasattr(info, "body")
    assert str(info) == "/recap a focus  — recap it"
