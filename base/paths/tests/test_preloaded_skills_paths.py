"""The preloaded-skills setting: its default, its environment parsing and its per-agent overlay."""

from __future__ import annotations

from pathlib import Path

import pytest

from base.config import FIELD_INFOS, AgentSettings, per_agent_field_names
from base.paths import skills_dir


@pytest.fixture(autouse=True)
def _isolate_load_dir(unit_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Run in a per-test unit home whose `skills/` does not exist by default so
    the real ~/.agents/skills/ never leaks into a scan; fake_skills_dir creates it."""

    def _all_enabled() -> set[str]:
        d = skills_dir()
        return {p.name for p in d.iterdir() if p.is_dir()} if d.is_dir() else set()

    monkeypatch.setattr(
        "base.packages.extensions.install_registry.loadable_skill_names", _all_enabled
    )


def test_field_default_is_empty() -> None:
    """The shipped default preloads nothing — opt-in per agent/preset."""
    factory = FIELD_INFOS["skills_to_expand_at_start"].default_factory
    assert factory is not None
    assert factory() == []  # type: ignore[call-arg]


def test_env_comma_string_parses() -> None:
    """The env form (a comma string) splits into stripped entries."""
    s = AgentSettings(AVA_SKILLS_TO_EXPAND_AT_START="ultra_speed, ava_memory.consolidation")  # pyright: ignore[reportArgumentType]
    assert s.skills_to_expand_at_start == ["ultra_speed", "ava_memory.consolidation"]


def test_field_is_per_agent_overridable() -> None:
    """A spawner must be able to overlay it onto one worker (like the index)."""
    assert "skills_to_expand_at_start" in per_agent_field_names()
