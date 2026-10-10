"""The memory index note the claim node reads: present, or none when absent, empty or disabled."""

from pathlib import Path
from typing import Any

import pytest

from base.agents.context import AvaContext
from base.clock import Clock
from base.config import settings
from base.host.env.agent_slices import AgentSlices


def _default_reader(domain: str, field: str) -> Any:
    return getattr(getattr(settings, domain), field)


def test_memory_index_note_present(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from ava_builtins.plugins.ava_memory import notes as _memory_inject

    monkeypatch.setattr(_memory_inject, "memory_dir", lambda: tmp_path)
    monkeypatch.setattr(settings.agent, "memory_index_inject_enabled", True)
    (tmp_path / "MEMORY.md").write_text("prod=~/.ava/source\n- people -> people/", encoding="utf-8")

    note = _memory_inject.memory_index_note(
        AvaContext(
            agent=AgentSlices.resolve(default_reader=_default_reader),
            clock_factory=Clock.from_settings,
        )
    )
    assert note is not None
    assert note.additional_kwargs["ava_msg_type"] == "system_note"  # pyright: ignore[reportUnknownMemberType]
    assert note.additional_kwargs["ava_note_tag"] == "memory"  # pyright: ignore[reportUnknownMemberType]
    assert (
        "prod=~/.ava/source" in note.content  # pyright: ignore[reportUnknownMemberType]
    )  # raw file content carried through


def test_memory_index_note_none_when_absent_empty_or_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ava_builtins.plugins.ava_memory import notes as _memory_inject

    monkeypatch.setattr(_memory_inject, "memory_dir", lambda: tmp_path)
    monkeypatch.setattr(settings.agent, "memory_index_inject_enabled", True)
    assert (
        _memory_inject.memory_index_note(
            AvaContext(
                agent=AgentSlices.resolve(default_reader=_default_reader),
                clock_factory=Clock.from_settings,
            )
        )
        is None
    )  # absent
    (tmp_path / "MEMORY.md").write_text("   \n\t\n", encoding="utf-8")
    assert (
        _memory_inject.memory_index_note(
            AvaContext(
                agent=AgentSlices.resolve(default_reader=_default_reader),
                clock_factory=Clock.from_settings,
            )
        )
        is None
    )  # whitespace-only
    (tmp_path / "MEMORY.md").write_text("real content", encoding="utf-8")
    monkeypatch.setattr(settings.agent, "memory_index_inject_enabled", False)
    assert (
        _memory_inject.memory_index_note(
            AvaContext(
                agent=AgentSlices.resolve(default_reader=_default_reader),
                clock_factory=Clock.from_settings,
            )
        )
        is None
    )  # disabled
