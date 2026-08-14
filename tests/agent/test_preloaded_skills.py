"""Tests for `skills_to_expand_at_start` — the preloaded-skills note.

Two surfaces:
- `resolve_prompt_skills` (agent/graph/_capabilities.py): the resolver shared
  with the capabilities index — wildcard, identifier-then-name, warn-and-skip.
- `preloaded_skills_note` (agent/graph/_memory_inject.py): the full-SKILL.md
  system note injected at cold start + after every compact (same carrier as the
  memory index).

Skills are faked by pointing `ava.skills._skills_dir` at a tmpdir and treating
every dir there as an enabled overlay entry — same shape as tests/ava/test_skills.py.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import ava.skills as skills_mod
from agent.graph._capabilities import resolve_prompt_skills
from agent.graph._context_notes import preloaded_skills_note
from shared.config import FIELD_INFOS, AgentSettings, per_agent_field_names, settings
from shared.message_kwargs import NoteTag


@pytest.fixture(autouse=True)
def _isolate_load_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Point _skills_dir at a non-existent path by default so the real
    ~/.agents/skills/ never leaks into a scan; fake_skills_dir re-points it."""
    monkeypatch.setattr(skills_mod, "_skills_dir", lambda: tmp_path / "no-skills")

    def _all_enabled() -> set[str]:
        d = skills_mod._skills_dir()
        return {p.name for p in d.iterdir() if p.is_dir()} if d.is_dir() else set()

    monkeypatch.setattr(skills_mod, "enabled_skill_names", _all_enabled)


@pytest.fixture
def fake_skills_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / "skills"
    d.mkdir()
    monkeypatch.setattr(skills_mod, "_skills_dir", lambda: d)
    return d


def _write_skill(root: Path, dirname: str, frontmatter: str, body: str = "") -> None:
    skill_dir = root / dirname
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(f"---\n{frontmatter}\n---\n\n{body}", encoding="utf-8")


def _expand(monkeypatch: pytest.MonkeyPatch, wanted: list[str]) -> None:
    monkeypatch.setattr(settings.agent, "skills_to_expand_at_start", wanted)


# ─── config field ─────────────────────────────────────────────────────────


def test_field_default_is_empty() -> None:
    """The shipped default preloads nothing — opt-in per agent/preset."""
    factory = FIELD_INFOS["skills_to_expand_at_start"].default_factory
    assert factory is not None
    assert factory() == []  # type: ignore[call-arg]


def test_env_comma_string_parses() -> None:
    """The env form (a comma string) splits into stripped entries."""
    s = AgentSettings(AVA_SKILLS_TO_EXPAND_AT_START="ultra_speed, ava_code.pr")  # pyright: ignore[reportArgumentType]
    assert s.skills_to_expand_at_start == ["ultra_speed", "ava_code.pr"]


def test_field_is_per_agent_overridable() -> None:
    """A spawner must be able to overlay it onto one worker (like the index)."""
    assert "skills_to_expand_at_start" in per_agent_field_names()


# ─── resolve_prompt_skills ─────────────────────────────────────────────────


def test_resolve_by_bare_name(fake_skills_dir: Path) -> None:
    _write_skill(fake_skills_dir, "ultra_speed", "name: ultra_speed\ndescription: go fast")
    resolved = resolve_prompt_skills(["ultra_speed"], config_field="skills_to_expand_at_start")
    assert [s["name"] for s in resolved] == ["ultra_speed"]


def test_resolve_by_dotted_identifier(fake_skills_dir: Path) -> None:
    """A namespaced skill resolves by its `.`-identifier, not just bare name."""
    parent = fake_skills_dir / "ava-code"
    _write_skill(parent, "pr", "name: pr\ndescription: open a PR")
    resolved = resolve_prompt_skills(["ava-code.pr"], config_field="skills_to_expand_at_start")
    assert [skills_mod.identifier(s) for s in resolved] == ["ava-code:pr"]


def test_resolve_accepts_the_python_spelling_of_a_dash_skill(fake_skills_dir: Path) -> None:
    """A config value still written in the underscore (Python) form resolves to
    the dash-named skill — the backcompat that keeps a preset row written before
    the rename working."""
    parent = fake_skills_dir / "ava-code"
    _write_skill(parent, "pr", "name: pr\ndescription: open a PR")
    resolved = resolve_prompt_skills(["ava_code.pr"], config_field="skills_to_expand_at_start")
    assert [skills_mod.identifier(s) for s in resolved] == ["ava-code:pr"]


def test_resolve_accepts_the_plugin_colon_spelling(fake_skills_dir: Path) -> None:
    """An ecosystem-style `plugin:skill` reference folds to the `.` form."""
    parent = fake_skills_dir / "ava-code"
    _write_skill(parent, "pr", "name: pr\ndescription: open a PR")
    resolved = resolve_prompt_skills(["ava-code:pr"], config_field="skills_to_expand_at_start")
    assert [skills_mod.identifier(s) for s in resolved] == ["ava-code:pr"]


def test_resolve_wildcard_selects_whole_catalog(fake_skills_dir: Path) -> None:
    _write_skill(fake_skills_dir, "alpha", "name: alpha\ndescription: a")
    _write_skill(fake_skills_dir, "beta", "name: beta\ndescription: b")
    resolved = resolve_prompt_skills(["*"], config_field="skills_to_expand_at_start")
    assert {s["name"] for s in resolved} == {"alpha", "beta"}


def test_resolve_unknown_name_warns_and_skips(
    fake_skills_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    _write_skill(fake_skills_dir, "real", "name: real\ndescription: r")
    with caplog.at_level("WARNING"):
        resolved = resolve_prompt_skills(
            ["real", "does_not_exist"], config_field="skills_to_expand_at_start"
        )
    assert [s["name"] for s in resolved] == ["real"]
    assert "does_not_exist" in caplog.text
    assert "skills_to_expand_at_start" in caplog.text  # warning names the config field


def test_resolve_empty_list_returns_empty(fake_skills_dir: Path) -> None:
    _write_skill(fake_skills_dir, "real", "name: real\ndescription: r")
    assert resolve_prompt_skills([], config_field="skills_to_expand_at_start") == []


def test_resolve_returns_empty_when_skills_sdk_disabled(
    fake_skills_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_skill(fake_skills_dir, "real", "name: real\ndescription: r")
    monkeypatch.setattr(settings.agent, "sdk_disable", ["skills"])
    assert resolve_prompt_skills(["real"], config_field="skills_to_expand_at_start") == []


# ─── preloaded_skills_note ─────────────────────────────────────────────────


def test_note_none_when_config_empty(
    fake_skills_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_skill(fake_skills_dir, "real", "name: real\ndescription: r")
    _expand(monkeypatch, [])
    assert preloaded_skills_note() is None


def test_note_none_when_nothing_resolves(
    fake_skills_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_skill(fake_skills_dir, "real", "name: real\ndescription: r")
    _expand(monkeypatch, ["ghost"])
    assert preloaded_skills_note() is None


def test_note_carries_full_body_and_tag(
    fake_skills_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = "# Ultra speed\n\nDo the fast thing. Never dawdle."
    _write_skill(
        fake_skills_dir, "ultra_speed", "name: ultra_speed\ndescription: go fast", body=body
    )
    _expand(monkeypatch, ["ultra_speed"])

    note = preloaded_skills_note()
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

    note = preloaded_skills_note()
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
    parent = fake_skills_dir / "ava_code"
    _write_skill(parent, "pr", "name: pr\ndescription: open a PR", body="pr playbook")
    _expand(monkeypatch, ["ava_code.pr"])

    note = preloaded_skills_note()
    assert note is not None
    assert isinstance(note.content, str)  # pyright: ignore[reportUnknownMemberType]
    assert "## ava.skills.ava-code:pr" in note.content
    assert "pr playbook" in note.content
