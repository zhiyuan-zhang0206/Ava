"""Tests for the system-prompt SDK-expand wildcard (`AVA_SDK_EXPAND="*"`).

`effective_sdk_expand()` turns the configured expand list into the concrete
paths the "# Expanded SDK reference" section renders. The `"*"` entry stands for
every top-level public ava namespace, discovered live from `help(ava)` so a new
namespace is covered without editing the default. These tests pin: what `"*"`
discovers (and what it deliberately skips — top-level functions, private names,
AVA_SDK_DISABLE entries, and the capability surfaces `skills` / `mcps` that the
`# Capabilities` section indexes), how it merges with explicit and
plugin-registered entries, that the legacy explicit-list format is untouched,
that the rendered section reflects the wildcard, and that the field default is
`["*"]`.
"""

import pytest

import ava
from agent.graph import _capabilities
from agent.graph._system_prompt import (
    _CAPABILITY_SURFACES,
    _discover_all_namespaces,
    _sdk_expand_section,
    effective_sdk_expand,
)
from shared.config import FIELD_INFOS, AgentSettings, settings

# The framework-owned top-level namespaces the wildcard must always surface.
# Asserted as a subset (not equality) so a plugin namespace registered into
# `ava.__all_for_ava__` during the session does not make these tests brittle.
# `skills` / `mcps` are deliberately absent — they are capability surfaces, not
# SDK API (see test_wildcard_skips_capability_surfaces).
FRAMEWORK_NAMESPACES = {
    "agents",
    "files",
    "self",
    "shell",
    "ui",
    "watcher",
    "web",
}


@pytest.fixture(autouse=True)
def _no_plugin_expansions(monkeypatch: pytest.MonkeyPatch) -> None:
    # Every case here reasons about the framework expand list alone; clear any
    # plugin-registered promotions so a leak from another test cannot prepend
    # stray paths. A test that needs a registration sets its own afterwards.
    monkeypatch.setattr(ava, "_REGISTERED_SDK_EXPANSIONS", [])


@pytest.fixture(autouse=True)
def _fresh_attribution_dedup(monkeypatch: pytest.MonkeyPatch) -> None:
    """`_recorded_skill_invocations` is per-agent-RUN state in a module global,
    so it leaks between tests: whichever test records (agent, skill, depth)
    first makes every later write a silent no-op — which would let the two
    "records nothing" assertions below pass without the guard they are pinning.
    Same fixture as tests/agent/test_capabilities_index.py."""
    monkeypatch.setattr(ava.skills, "_recorded_skill_invocations", set())  # pyright: ignore[reportUnknownArgumentType]


def test_wildcard_expands_all_public_namespaces(monkeypatch: pytest.MonkeyPatch) -> None:
    """`["*"]` resolves to exactly the discovered namespaces — every
    framework namespace, in deterministic sorted order, and nothing the
    discovery would not surface."""
    monkeypatch.setattr(settings.agent, "sdk_expand_in_system_prompt", ["*"])
    result = effective_sdk_expand()
    assert set(result) >= FRAMEWORK_NAMESPACES
    assert "shell.sessions" in result  # nested namespace discovered recursively
    assert result == _discover_all_namespaces()
    assert result == sorted(result)  # deterministic order


def test_wildcard_excludes_functions_and_private_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Top-level functions (`help`, `understand`) and private names are not
    namespaces — the SDK overview already prints functions in full, so the
    wildcard skips them rather than duplicating their contract."""
    monkeypatch.setattr(settings.agent, "sdk_expand_in_system_prompt", ["*"])
    result = effective_sdk_expand()
    assert "help" not in result
    assert "understand" not in result
    assert not any(name.startswith("_") for name in result)


def test_wildcard_skips_capability_surfaces(monkeypatch: pytest.MonkeyPatch) -> None:
    """`skills` and `mcps` are capability surfaces, not SDK API: expanding them
    renders a full listing of every installed skill / configured server, which
    is exactly what `# Capabilities` already indexes. `"*"` skips both so the
    prompt carries one index, not two."""
    monkeypatch.setattr(settings.agent, "sdk_expand_in_system_prompt", ["*"])
    result = effective_sdk_expand()
    assert {"skills", "mcps"} == _CAPABILITY_SURFACES
    assert not (_CAPABILITY_SURFACES & set(result))
    text = _sdk_expand_section()
    assert "## ava.skills" not in text
    assert "## ava.mcps" not in text


def test_capability_surface_expands_when_named_explicitly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The skip is a wildcard default, not a ban — an operator who names a
    capability SURFACE explicitly still gets it expanded. Expanding the surface
    is index-style, so it costs a duplicate index and nothing worse: no SKILL.md
    body reaches the prompt and no attribution is recorded."""
    monkeypatch.setattr(settings.agent, "sdk_expand_in_system_prompt", ["*", "skills"])
    recorded: list[tuple[int, list]] = []
    monkeypatch.setattr(
        ava.skills,
        "_insert_skill_events",
        lambda agent, skills: recorded.append((agent, skills)),  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
    )

    assert "skills" in effective_sdk_expand()
    text = _sdk_expand_section()
    assert "## ava.skills" in text
    assert recorded == []
    # An index render carries descriptions, never bodies. Every SKILL.md in this
    # repo opens with a markdown heading; none may appear under the skills block.
    skills_block = text[text.index("## ava.skills") :]
    skills_block = skills_block.split("\n## ava.")[0]
    assert "\n# " not in skills_block


def test_member_of_a_capability_surface_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`skills.<name>` / `mcps.<server>` are refused even when named explicitly.

    Resolving one walks plain getattr down to a single skill / server, which is
    the "agent opened this" path: it would inline that skill's whole SKILL.md
    into every prompt and record a depth="loaded" attribution for a skill nobody
    read. A skill body belongs in skills_to_expand_at_start, which is accounted
    for as prompt cost.

    The refusal lives in `effective_sdk_expand`, so it holds for every consumer
    of that view (the ava_code section reads it too), not only for the section
    that renders it."""
    monkeypatch.setattr(
        settings.agent, "sdk_expand_in_system_prompt", ["*", "skills.gmail", "mcps.chrome"]
    )
    recorded: list[tuple[int, list]] = []
    monkeypatch.setattr(
        ava.skills,
        "_insert_skill_events",
        lambda agent, skills: recorded.append((agent, skills)),  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
    )

    assert "skills.gmail" not in effective_sdk_expand()
    assert "mcps.chrome" not in effective_sdk_expand()
    text = _sdk_expand_section()
    assert "## ava.skills.gmail" not in text
    assert "## ava.mcps.chrome" not in text
    assert recorded == []


def test_wildcard_respects_sdk_disable(monkeypatch: pytest.MonkeyPatch) -> None:
    """A namespace listed in AVA_SDK_DISABLE is dropped from the wildcard set —
    a disabled namespace must never be expanded back into the prompt."""
    monkeypatch.setattr(settings.agent, "sdk_expand_in_system_prompt", ["*"])
    monkeypatch.setattr(settings.agent, "sdk_disable", ["watcher"])
    result = effective_sdk_expand()
    assert "watcher" not in result
    assert "files" in result and "self" in result


def test_wildcard_disable_of_member_keeps_namespace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Disabling a member (`self.terminate`) removes only that attribute; the
    parent namespace still expands under the wildcard."""
    monkeypatch.setattr(settings.agent, "sdk_expand_in_system_prompt", ["*"])
    monkeypatch.setattr(settings.agent, "sdk_disable", ["self.terminate"])
    assert "self" in effective_sdk_expand()


def test_wildcard_merges_with_explicit_nested_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`["*", "shell.sessions"]` is a union: the wildcard expands every
    top-level and nested namespace; the explicit `shell.sessions` is deduped
    (keep-first from the wildcard block), so `shell` sorts before `shell.sessions`."""
    monkeypatch.setattr(settings.agent, "sdk_expand_in_system_prompt", ["*", "shell.sessions"])
    result = effective_sdk_expand()
    assert "shell.sessions" in result
    assert set(result) >= FRAMEWORK_NAMESPACES
    assert result.index("shell") < result.index("shell.sessions")


def test_wildcard_dedups_overlapping_explicit_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A namespace named explicitly AND discovered by `*` appears once, keeping
    its first (explicit) position."""
    monkeypatch.setattr(settings.agent, "sdk_expand_in_system_prompt", ["files", "*"])
    result = effective_sdk_expand()
    assert result.count("files") == 1
    assert result[0] == "files"


def test_legacy_explicit_list_is_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    """A list with no `"*"` is the old behavior: returned verbatim, deduped
    keep-first, no discovery."""
    monkeypatch.setattr(settings.agent, "sdk_expand_in_system_prompt", ["files", "shell", "self"])
    assert effective_sdk_expand() == ["files", "shell", "self"]


def test_missing_unregistered_expand_path_warns(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """An unregistered missing path reaches the existing resolution warning."""
    monkeypatch.setattr(settings.agent, "sdk_expand_in_system_prompt", ["missing_sdk_namespace"])
    monkeypatch.setattr(settings.agent, "sdk_disable", [])
    monkeypatch.setattr(ava, "_applied_disable_entries", set())

    with caplog.at_level("WARNING", logger=_capabilities.__name__):
        text = _sdk_expand_section()

    assert text == ""
    assert "ava.missing_sdk_namespace does not resolve" in caplog.text


def test_plugin_registrations_lead_the_wildcard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Plugin-promoted paths keep their lead position ahead of the discovered
    set even when the configured list is just `["*"]`."""
    monkeypatch.setattr(ava, "_REGISTERED_SDK_EXPANSIONS", ["cwd"])
    monkeypatch.setattr(settings.agent, "sdk_expand_in_system_prompt", ["*"])
    result = effective_sdk_expand()
    assert result[0] == "cwd"
    assert set(result) >= FRAMEWORK_NAMESPACES


def test_section_renders_all_wildcard_namespaces(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The rendered section carries one `## ava.<name>` contract per discovered
    namespace and no heading for the skipped top-level functions."""
    monkeypatch.setattr(settings.agent, "sdk_expand_in_system_prompt", ["*"])
    text = _sdk_expand_section()
    assert text.startswith("# Expanded SDK reference")
    for ns in FRAMEWORK_NAMESPACES:
        assert f"## ava.{ns}" in text
    assert "## ava.understand" not in text
    assert "## ava.help" not in text
    assert "## ava.shell.sessions" in text  # nested namespace rendered


def test_field_default_is_wildcard() -> None:
    """The shipped default is `["*"]` — expand everything by default."""
    factory = FIELD_INFOS["sdk_expand_in_system_prompt"].default_factory
    assert factory is not None
    assert factory() == ["*"]  # type: ignore[call-arg]


def test_env_comma_string_with_wildcard_parses() -> None:
    """The env form (a comma string) splits into entries; `"*"` survives as its
    own entry and combines with an explicit nested path."""
    s = AgentSettings(AVA_SDK_EXPAND="*,shell.sessions")  # pyright: ignore[reportArgumentType]  # env-form str; validator splits to list
    assert s.sdk_expand_in_system_prompt == ["*", "shell.sessions"]


def test_section_hides_attach_for_text_only_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A text-only model's expanded SDK reference carries no attach contract —
    the member is unavailable to it (user ruling 2026-08-28)."""
    monkeypatch.setattr(settings.agent, "sdk_expand_in_system_prompt", ["*"])
    monkeypatch.setattr(settings.lm, "llm_model", "deepseek-v4-pro")
    text = _sdk_expand_section()
    assert "## ava.self" in text
    assert "def attach(" not in text


def test_section_keeps_attach_for_media_capable_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A media-capable model's expanded SDK reference keeps the attach
    contract unchanged (user ruling 2026-08-28)."""
    monkeypatch.setattr(settings.agent, "sdk_expand_in_system_prompt", ["*"])
    monkeypatch.setattr(settings.lm, "llm_model", "deepseek-v4-flash-vision-exp")
    text = _sdk_expand_section()
    assert "## ava.self" in text
    assert "def attach(" in text
