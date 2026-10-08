"""Tests for `skills_to_expand_at_start` — the preloaded-skills note.

Two surfaces:
- `resolve_prompt_skills` (agent/graph/prompt/capabilities.py): the resolver shared
  with the capabilities index — wildcard, identifier-then-name, warn-and-skip.
- `preloaded_skills_note` (agent/graph/_memory_inject.py): the full-SKILL.md
  system note injected at cold start + after every compact (same carrier as the
  memory index).

Skills are faked by running in a per-test unit home (`unit_home`) and treating
every dir under `<home>/skills` as an enabled overlay entry — same shape as
ava/tests/skills/test_skills.py.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import ava.skills as skills_mod
from agent.graph.prompt.capabilities import resolve_prompt_skills
from agent.graph.prompt.context_notes import preloaded_skills_note
from base.agents.context import AvaContext
from base.agents.messages.kwargs import NoteTag
from base.config import settings
from base.host.env.agent_slices import AgentSlices
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


@pytest.fixture
def fake_skills_dir(unit_home: Path) -> Path:
    d = skills_dir()
    d.mkdir()
    return d


def _write_skill(root: Path, dirname: str, frontmatter: str, body: str = "") -> None:
    skill_dir = root / dirname
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(f"---\n{frontmatter}\n---\n\n{body}", encoding="utf-8")


def _expand(monkeypatch: pytest.MonkeyPatch, wanted: list[str]) -> None:
    monkeypatch.setattr(settings.agent, "skills_to_expand_at_start", wanted)


# ─── config field ─────────────────────────────────────────────────────────


# ─── resolve_prompt_skills ─────────────────────────────────────────────────


def test_resolve_by_bare_name(fake_skills_dir: Path) -> None:
    _write_skill(fake_skills_dir, "ultra_speed", "name: ultra_speed\ndescription: go fast")
    resolved = resolve_prompt_skills(
        ["ultra_speed"],
        AgentSlices.resolve().prompt.sdk_disable,
        config_field="skills_to_expand_at_start",
    )
    assert [s["name"] for s in resolved] == ["ultra_speed"]


def test_resolve_by_dotted_identifier(fake_skills_dir: Path) -> None:
    """A namespaced skill resolves by its `.`-identifier, not just bare name."""
    parent = fake_skills_dir / "ava-memory"
    _write_skill(parent, "consolidation", "name: consolidation\ndescription: merge notes")
    resolved = resolve_prompt_skills(
        ["ava-memory.consolidation"],
        AgentSlices.resolve().prompt.sdk_disable,
        config_field="skills_to_expand_at_start",
    )
    assert [skills_mod.identifier(s) for s in resolved] == ["ava-memory:consolidation"]


def test_resolve_accepts_the_python_spelling_of_a_dash_skill(fake_skills_dir: Path) -> None:
    """A config value still written in the underscore (Python) form resolves to
    the dash-named skill — the backcompat that keeps a preset row written before
    the rename working."""
    parent = fake_skills_dir / "ava-memory"
    _write_skill(parent, "consolidation", "name: consolidation\ndescription: merge notes")
    resolved = resolve_prompt_skills(
        ["ava_memory.consolidation"],
        AgentSlices.resolve().prompt.sdk_disable,
        config_field="skills_to_expand_at_start",
    )
    assert [skills_mod.identifier(s) for s in resolved] == ["ava-memory:consolidation"]


def test_resolve_accepts_the_plugin_colon_spelling(fake_skills_dir: Path) -> None:
    """An ecosystem-style `plugin:skill` reference folds to the `.` form."""
    parent = fake_skills_dir / "ava-memory"
    _write_skill(parent, "consolidation", "name: consolidation\ndescription: merge notes")
    resolved = resolve_prompt_skills(
        ["ava-memory:consolidation"],
        AgentSlices.resolve().prompt.sdk_disable,
        config_field="skills_to_expand_at_start",
    )
    assert [skills_mod.identifier(s) for s in resolved] == ["ava-memory:consolidation"]


def test_resolve_wildcard_selects_whole_catalog(fake_skills_dir: Path) -> None:
    _write_skill(fake_skills_dir, "alpha", "name: alpha\ndescription: a")
    _write_skill(fake_skills_dir, "beta", "name: beta\ndescription: b")
    resolved = resolve_prompt_skills(
        ["*"], AgentSlices.resolve().prompt.sdk_disable, config_field="skills_to_expand_at_start"
    )
    assert {s["name"] for s in resolved} == {"alpha", "beta"}


def test_resolve_unknown_name_warns_and_skips(
    fake_skills_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    _write_skill(fake_skills_dir, "real", "name: real\ndescription: r")
    with caplog.at_level("WARNING"):
        resolved = resolve_prompt_skills(
            ["real", "does_not_exist"],
            AgentSlices.resolve().prompt.sdk_disable,
            config_field="skills_to_expand_at_start",
        )
    assert [s["name"] for s in resolved] == ["real"]
    assert "does_not_exist" in caplog.text
    assert "skills_to_expand_at_start" in caplog.text  # warning names the config field


def test_resolve_empty_list_returns_empty(fake_skills_dir: Path) -> None:
    _write_skill(fake_skills_dir, "real", "name: real\ndescription: r")
    assert (
        resolve_prompt_skills(
            [], AgentSlices.resolve().prompt.sdk_disable, config_field="skills_to_expand_at_start"
        )
        == []
    )


def test_resolve_returns_empty_when_skills_sdk_disabled(
    fake_skills_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_skill(fake_skills_dir, "real", "name: real\ndescription: r")
    monkeypatch.setattr(settings.agent, "sdk_disable", ["skills"])
    assert (
        resolve_prompt_skills(
            ["real"],
            AgentSlices.resolve().prompt.sdk_disable,
            config_field="skills_to_expand_at_start",
        )
        == []
    )


# ─── preloaded_skills_note ─────────────────────────────────────────────────


def test_note_none_when_config_empty(
    fake_skills_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_skill(fake_skills_dir, "real", "name: real\ndescription: r")
    _expand(monkeypatch, [])
    assert preloaded_skills_note(AvaContext(agent=AgentSlices.resolve())) is None


def test_note_none_when_nothing_resolves(
    fake_skills_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_skill(fake_skills_dir, "real", "name: real\ndescription: r")
    _expand(monkeypatch, ["ghost"])
    assert preloaded_skills_note(AvaContext(agent=AgentSlices.resolve())) is None


def test_note_carries_full_body_and_tag(
    fake_skills_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = "# Ultra speed\n\nDo the fast thing. Never dawdle."
    _write_skill(
        fake_skills_dir, "ultra_speed", "name: ultra_speed\ndescription: go fast", body=body
    )
    _expand(monkeypatch, ["ultra_speed"])

    note = preloaded_skills_note(AvaContext(agent=AgentSlices.resolve()))
    assert note is not None
    assert isinstance(note.content, str)  # pyright: ignore[reportUnknownMemberType]
    content = note.content
    # system_note_message prefixes "[system] "; framing + PRELOADED_SKILLS tag.
    assert content.startswith("[system] Preloaded skills")
    assert note.additional_kwargs["ava_note_tag"] == NoteTag.PRELOADED_SKILLS.value  # pyright: ignore[reportUnknownMemberType]
    # The access-path heading + the full SKILL.md body (frontmatter included, as
    # ava.help renders a skill) are both present.
    assert "## ava.skills.ultra-speed" in content
    assert "Do the fast thing. Never dawdle." in content
    assert "description: go fast" in content  # frontmatter is part of the full text


def test_note_merges_multiple_skills_in_order(
    fake_skills_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_skill(fake_skills_dir, "first", "name: first\ndescription: 1", body="AAA body")
    _write_skill(fake_skills_dir, "second", "name: second\ndescription: 2", body="BBB body")
    _expand(monkeypatch, ["second", "first"])  # explicit order preserved

    note = preloaded_skills_note(AvaContext(agent=AgentSlices.resolve()))
    assert note is not None
    assert isinstance(note.content, str)  # pyright: ignore[reportUnknownMemberType]
    content = note.content
    # One note, both bodies, a `---` separator between the two sections.
    assert "AAA body" in content and "BBB body" in content
    assert "\n---\n" in content
    # Requested order (second before first) is the render order.
    assert content.index("## ava.skills.second") < content.index("## ava.skills.first")


def test_note_heading_uses_dotted_access_path(
    fake_skills_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A namespaced skill's heading is its ava.skills access path (attr form)."""
    parent = fake_skills_dir / "ava_memory"
    _write_skill(
        parent,
        "consolidation",
        "name: consolidation\ndescription: merge notes",
        body="consolidation playbook",
    )
    _expand(monkeypatch, ["ava_memory.consolidation"])

    note = preloaded_skills_note(AvaContext(agent=AgentSlices.resolve()))
    assert note is not None
    assert isinstance(note.content, str)  # pyright: ignore[reportUnknownMemberType]
    assert "## ava.skills.ava-memory:consolidation" in note.content
    assert "consolidation playbook" in note.content
