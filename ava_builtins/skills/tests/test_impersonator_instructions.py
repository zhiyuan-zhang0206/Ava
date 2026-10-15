import sys
from typing import Any

import pytest

from ava_builtins.skills.tests.skill_script import load_skill_script


@pytest.fixture
def instruction_reader() -> Any:
    return load_skill_script(
        "platform",
        "impersonator-guide",
        "scripts",
        "read_instructions.py",
        name="impersonator_instruction_reader",
    )


def test_instruction_script_selects_native_prompt_and_preloaded_skills(
    instruction_reader: Any,
) -> None:
    from langchain_core.messages import HumanMessage, SystemMessage

    from agent.messages import NoteTag, system_note_message

    messages = [
        SystemMessage(content="Native core and plugin rules\n"),
        system_note_message(content="Memory index", tag=NoteTag.MEMORY),
        system_note_message(content="Preloaded skill", tag=NoteTag.PRELOADED_SKILLS),
        HumanMessage(content="Task history"),
        system_note_message(content="Fork-added skill", tag=NoteTag.PRELOADED_SKILLS),
    ]
    assert instruction_reader.instruction_text(messages) == (
        "Native core and plugin rules\n\n\n[system] Preloaded skill\n\n[system] Fork-added skill"
    )


@pytest.mark.parametrize("content", [None, "", [{"type": "text", "text": "rules"}]])
def test_instruction_script_refuses_missing_or_nontext_prompt(
    instruction_reader: Any, content: Any
) -> None:
    from langchain_core.messages import SystemMessage

    messages = [] if content is None else [SystemMessage(content=content)]
    with pytest.raises((RuntimeError, ValueError), match=r"system prompt|nonempty text"):
        instruction_reader.instruction_text(messages)


def test_instruction_script_prints_only_after_successful_detach(
    instruction_reader: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from contextlib import contextmanager
    from types import SimpleNamespace

    from langchain_core.messages import SystemMessage

    @contextmanager
    def attach(session_id: int, *, agent_id: int) -> Any:
        assert (session_id, agent_id) == (0, 405)
        yield SimpleNamespace(
            native_snapshot=lambda: SimpleNamespace(
                messages=[SystemMessage(content="Native rules")]
            )
        )
        raise RuntimeError("lease expired before detach")

    monkeypatch.setattr(instruction_reader.impersonation, "attach", attach)
    monkeypatch.setattr(sys, "argv", ["read_instructions.py", "0", "--agent", "405"])
    with pytest.raises(RuntimeError, match="lease expired"):
        instruction_reader.main()
    assert capsys.readouterr().out == ""
