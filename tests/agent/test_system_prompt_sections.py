"""Gating logic for framework-owned system-prompt sections.

`_beyond_task_section` is gated solely by `agent_reflection_enabled`
(AVA_AGENT_REFLECTION). The section renders when it is on and is
suppressed when it is off — reflection and memory are separate concerns.

`_communication_style_section` is the odd one out: it is selected, not gated —
'oriented' / 'concise' / 'silent' always render something — except for the
fourth member, 'off', which is a gate like any other and renders nothing.
"""

import pytest

from agent.graph._system_prompt import _beyond_task_section, _communication_style_section
from shared.config import settings


@pytest.mark.parametrize(
    ("enabled", "expect_section"),
    [
        (True, True),
        (False, False),
    ],
)
def test_beyond_task_section_reflection_gating(
    monkeypatch: pytest.MonkeyPatch, enabled, expect_section
):
    monkeypatch.setattr(settings.agent, "agent_reflection_enabled", enabled)  # pyright: ignore[reportUnknownArgumentType]

    rendered = _beyond_task_section()

    if expect_section:
        assert "Beyond the task at hand" in rendered
    else:
        assert rendered == ""


def test_beyond_task_section_mines_intent_into_tasks(monkeypatch: pytest.MonkeyPatch):
    """When it renders, the section directs fleet-context agents to persist
    worthwhile follow-ups as open tasks (with provenance), not just offer them —
    the intent-mining convention. Standalone agents keep the user-only path."""
    monkeypatch.setattr(settings.agent, "agent_reflection_enabled", True)

    rendered = _beyond_task_section()

    assert "open tasks in the registry" in rendered
    assert "standalone agent" in rendered.lower()


@pytest.mark.parametrize(
    ("enabled", "expect_section"),
    [
        (True, True),
        (False, False),
    ],
)
def test_temporal_awareness_section_gating(
    monkeypatch: pytest.MonkeyPatch, enabled, expect_section
):
    monkeypatch.setattr(settings.agent, "prompt_temporal_awareness_enabled", enabled)  # pyright: ignore[reportUnknownArgumentType]

    from agent.graph._system_prompt import _temporal_awareness_section

    rendered = _temporal_awareness_section()

    if expect_section:
        assert "Temporal awareness" in rendered
    else:
        assert rendered == ""


@pytest.mark.parametrize(
    ("enabled", "expect_section"),
    [
        (True, True),
        (False, False),
    ],
)
def test_keep_it_simple_section_gating(monkeypatch: pytest.MonkeyPatch, enabled, expect_section):
    monkeypatch.setattr(settings.agent, "prompt_keep_it_simple_enabled", enabled)  # pyright: ignore[reportUnknownArgumentType]

    from agent.graph._system_prompt import _keep_it_simple_section

    rendered = _keep_it_simple_section()

    if expect_section:
        assert "Keep It Simple" in rendered
    else:
        assert rendered == ""


def test_keep_it_simple_section_carries_meta_principle(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings.agent, "prompt_keep_it_simple_enabled", True)

    from agent.graph._system_prompt import _keep_it_simple_section

    rendered = _keep_it_simple_section()

    assert "Keep It Simple" in rendered
    assert "looks cheaper" in rendered and "conceptually simpler" in rendered
    assert "favor the principle even when it is tedious" in rendered
    assert "this meta-principle decides" in rendered


# --- CodeAct batching (AVA_SYSTEM_PROMPT_CODEACT, off by default) ---


def test_codeact_section_defaults_to_off():
    """The CodeAct section is opt-in: the flag defaults to False, so an
    unconfigured cluster never pays for the section — unlike the
    on-by-default behavioral sections."""
    assert settings.agent.prompt_codeact_enabled is False


@pytest.mark.parametrize(
    ("enabled", "expect_section"),
    [
        (True, True),
        (False, False),
    ],
)
def test_codeact_section_gating(monkeypatch: pytest.MonkeyPatch, enabled, expect_section):
    monkeypatch.setattr(settings.agent, "prompt_codeact_enabled", enabled)  # pyright: ignore[reportUnknownArgumentType]

    from agent.graph._codeact import _codeact_section

    rendered = _codeact_section()

    if expect_section:
        assert "CodeAct" in rendered
    else:
        assert rendered == ""


def test_codeact_section_urges_batching(monkeypatch: pytest.MonkeyPatch):
    """When it renders, the section pushes the batching behavior by name:
    several operations per call, batch file reads, branches folded into one
    script, and the cost model (one call = one API round-trip) that justifies
    it."""
    monkeypatch.setattr(settings.agent, "prompt_codeact_enabled", True)

    from agent.graph._codeact import _codeact_section

    rendered = _codeact_section()

    assert "execute_code" in rendered
    assert "one LLM API round-trip" in rendered
    assert "several files in one call" in rendered
    assert "if-else" in rendered
    assert "round-trips" in rendered


def test_codeact_section_in_full_prompt_when_on(monkeypatch: pytest.MonkeyPatch):
    """End-to-end: with the toggle on, the section is part of the assembled
    system prompt."""
    monkeypatch.setattr(settings.agent, "prompt_codeact_enabled", True)

    from agent.graph._system_prompt import build_system_prompt

    prompt = build_system_prompt()

    assert "CodeAct" in prompt


def test_codeact_section_absent_from_full_prompt_when_off(monkeypatch: pytest.MonkeyPatch):
    """End-to-end: with the toggle off (the default), the section is gone from
    the assembled prompt entirely."""
    monkeypatch.setattr(settings.agent, "prompt_codeact_enabled", False)

    from agent.graph._system_prompt import build_system_prompt

    prompt = build_system_prompt()

    assert "CodeAct" not in prompt


def test_temporal_awareness_invokes_ai_capability_timescale(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(settings.agent, "prompt_temporal_awareness_enabled", True)

    from agent.graph._system_prompt import _temporal_awareness_section

    rendered = _temporal_awareness_section()

    assert "ai-capability-timescale" in rendered
    assert "scheduling, estimating, or judging the feasibility" in rendered


@pytest.mark.parametrize(
    ("enabled", "expect_section"),
    [
        (True, True),
        (False, False),
    ],
)
def test_ui_delivery_section_gating(monkeypatch: pytest.MonkeyPatch, enabled, expect_section):
    monkeypatch.setattr(settings.agent, "prompt_ui_delivery_enabled", enabled)  # pyright: ignore[reportUnknownArgumentType]

    from agent.graph._system_prompt import _ui_delivery_section

    rendered = _ui_delivery_section()

    if expect_section:
        assert "Deliver through the UI" in rendered
    else:
        assert rendered == ""


def test_ui_delivery_section_prefers_ui_over_file_paths(monkeypatch: pytest.MonkeyPatch):
    """The section states the semantic rule — present content through the UI,
    call out the anti-pattern by name (a Markdown file plus its path as the
    presentation channel) — and keeps files their role as persistence and
    handoff. It names NO concrete SDK entry points: the rule is the stable
    part, function signatures are not, so the section must stay example-free."""
    monkeypatch.setattr(settings.agent, "prompt_ui_delivery_enabled", True)

    from agent.graph._system_prompt import _ui_delivery_section

    rendered = _ui_delivery_section()

    assert "through the UI" in rendered
    assert "telling the user its path" in rendered
    assert "persistence" in rendered and "other agents" in rendered
    assert "ava.ui." not in rendered  # example-free: no SDK function names


# --- Communication style ---


@pytest.mark.parametrize("style", ["oriented", "concise", "silent"])
def test_communication_style_always_renders_the_channel_map(
    monkeypatch: pytest.MonkeyPatch, style
) -> None:
    """Which channel actually reaches the user is a fact about the system, so
    every style carries it — the style only picks the narration guidance."""
    monkeypatch.setattr(settings.agent, "agent_communication_style", style)  # pyright: ignore[reportUnknownArgumentType]

    rendered = _communication_style_section()

    assert rendered.startswith("# ")
    assert "`ava.ui.notify`" in rendered
    assert "Code output" in rendered and "Text content" in rendered


def test_oriented_style_keeps_the_user_oriented(monkeypatch: pytest.MonkeyPatch) -> None:
    """The default style is the historical section: interleave brief updates,
    surface direction changes, flag blockers early."""
    monkeypatch.setattr(settings.agent, "agent_communication_style", "oriented")

    rendered = _communication_style_section()

    assert rendered.startswith("# Keeping the user oriented")
    assert "don't work in long silences" in rendered


def test_silent_style_asks_for_no_narration_and_a_closing_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Silent means quiet *while working* plus one standalone report at the end —
    not silence about the outcome. Blockers still interrupt immediately."""
    monkeypatch.setattr(settings.agent, "agent_communication_style", "silent")

    rendered = _communication_style_section()

    assert "Work without narrating" in rendered
    assert "one complete report" in rendered
    assert "blocker" in rendered
    # The oriented guidance is gone, not merely appended to.
    assert "Keeping the user oriented" not in rendered
    assert "don't work in long silences" not in rendered


def test_concise_style_speaks_only_at_milestones(monkeypatch: pytest.MonkeyPatch) -> None:
    """Between milestones the agent works without commenting — and neither of
    the other two styles' guidance leaks in."""
    monkeypatch.setattr(settings.agent, "agent_communication_style", "concise")

    rendered = _communication_style_section()

    assert "Speak at milestones" in rendered
    assert "Keeping the user oriented" not in rendered
    assert "Work without narrating" not in rendered


def test_every_narrating_style_renders_a_distinct_section() -> None:
    """The Literal members minus 'off' and the rendered-section keys are the
    same set, and no two styles produce the same text — a missing style would
    KeyError at render time rather than silently fall back."""
    from typing import get_args

    from agent.graph._system_prompt import _COMMUNICATION_STYLE_SECTIONS
    from shared.config.agent import AgentSettings

    annotation = AgentSettings.model_fields["agent_communication_style"].annotation
    # The field is None-sentinel'd (`Literal[...] | None` — unset resolves the
    # per-model default); unwrap the union to the Literal member set.
    literal = next(a for a in get_args(annotation) if a is not type(None))
    members = set(get_args(literal))
    assert set(_COMMUNICATION_STYLE_SECTIONS) == members - {"off"}
    assert len(set(_COMMUNICATION_STYLE_SECTIONS.values())) == len(_COMMUNICATION_STYLE_SECTIONS)


def test_off_style_omits_the_section_entirely(monkeypatch: pytest.MonkeyPatch) -> None:
    """'off' is a gate, not a narration choice: no channel map, no style body,
    nothing — an empty return, which register_system_prompt_section treats as
    no contribution to the assembled system prompt."""
    monkeypatch.setattr(settings.agent, "agent_communication_style", "off")

    rendered = _communication_style_section()

    assert rendered == ""


def test_off_style_is_absent_from_the_full_system_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    """End-to-end: build_system_prompt() carries no trace of the communication
    style section when the style is 'off' — not just the leaf function."""
    monkeypatch.setattr(settings.agent, "agent_communication_style", "off")
    from agent.graph._system_prompt import build_system_prompt

    prompt = build_system_prompt()

    assert "# Keeping the user oriented" not in prompt
    assert "# Talking to the user" not in prompt


# --- Knowledge cutoff ---


def test_knowledge_cutoff_appears_for_known_model(monkeypatch: pytest.MonkeyPatch):
    """When the current model has an entry in MODEL_KNOWLEDGE_CUTOFF,
    build_system_prompt() appends a 'Knowledge cutoff: YYYY-MM' line."""
    monkeypatch.setattr(settings.lm, "llm_model", "claude-sonnet-5")
    from agent.graph._system_prompt import build_system_prompt

    prompt = build_system_prompt()
    assert "Knowledge cutoff: 2026-01" in prompt


def test_knowledge_cutoff_absent_for_unknown_model(monkeypatch: pytest.MonkeyPatch):
    """When the model is not in MODEL_KNOWLEDGE_CUTOFF, no cutoff line is
    appended — the prompt just omits it rather than crashing."""
    monkeypatch.setattr(settings.lm, "llm_model", "unknown-model-v1")
    from agent.graph._system_prompt import build_system_prompt

    prompt = build_system_prompt()
    assert "Knowledge cutoff:" not in prompt


def test_model_knowledge_cutoff_all_entries_valid():
    """Every entry in MODEL_KNOWLEDGE_CUTOFF is a YYYY-MM string."""
    import re

    from shared.lm.factory import MODEL_KNOWLEDGE_CUTOFF

    assert len(MODEL_KNOWLEDGE_CUTOFF) > 0
    for model, cutoff in MODEL_KNOWLEDGE_CUTOFF.items():
        assert re.match(r"^\d{4}-\d{2}$", cutoff), f"Bad format for {model}: {cutoff!r}"


def test_supported_models_all_have_cutoff(monkeypatch: pytest.MonkeyPatch):
    """Every model in SUPPORTED_MODELS has an entry in MODEL_KNOWLEDGE_CUTOFF."""
    from shared.lm.factory import MODEL_KNOWLEDGE_CUTOFF, SUPPORTED_MODELS

    for models in SUPPORTED_MODELS.values():
        for model in models:
            assert model in MODEL_KNOWLEDGE_CUTOFF, (
                f"{model!r} is in SUPPORTED_MODELS but missing from MODEL_KNOWLEDGE_CUTOFF"
            )


# --- Cross-machine delegation hint ---

# User-finalized wording, shipped verbatim — do not "improve" it.
_CROSS_MACHINE_DELEGATION_SENTENCE = (
    "When working across different machines, consider spawning an agent on "
    "the target machine and let it do the work for you, as it can access the "
    "machine's resources directly."
)


@pytest.mark.parametrize(
    ("enabled", "expect_section"),
    [
        (True, True),
        (False, False),
    ],
)
def test_cross_machine_delegation_section_gating(
    monkeypatch: pytest.MonkeyPatch, enabled, expect_section
):
    monkeypatch.setattr(settings.agent, "prompt_cross_machine_delegation_enabled", enabled)  # pyright: ignore[reportUnknownArgumentType]

    from agent.graph._system_prompt import _cross_machine_delegation_section

    rendered = _cross_machine_delegation_section()

    if expect_section:
        assert rendered == _CROSS_MACHINE_DELEGATION_SENTENCE
    else:
        assert rendered == ""


def test_cross_machine_delegation_section_is_verbatim(monkeypatch: pytest.MonkeyPatch):
    """The section renders the user-finalized sentence exactly — a wording
    change anywhere in the sentence fails this test on purpose."""
    monkeypatch.setattr(settings.agent, "prompt_cross_machine_delegation_enabled", True)

    from agent.graph._system_prompt import _cross_machine_delegation_section

    rendered = _cross_machine_delegation_section()

    assert rendered == _CROSS_MACHINE_DELEGATION_SENTENCE
    assert rendered.count("machine") == 3


def test_cross_machine_delegation_section_names_no_api_detail(monkeypatch: pytest.MonkeyPatch):
    """Semantic steer only: no SDK function signatures (no `spawn(...)`),
    no concrete mechanism (no SSH) — the stable part is the rule, not the
    API, so the section must stay mechanism-free (user preference: system
    prompt sections describe semantics, not call syntax)."""
    monkeypatch.setattr(settings.agent, "prompt_cross_machine_delegation_enabled", True)

    from agent.graph._system_prompt import _cross_machine_delegation_section

    rendered = _cross_machine_delegation_section()

    assert "spawn(" not in rendered
    assert "ssh" not in rendered.lower()


def test_cross_machine_delegation_hint_in_full_prompt_when_on(monkeypatch: pytest.MonkeyPatch):
    """End-to-end: with the toggle on (the default), the sentence is part of
    the assembled system prompt, rendered after the delegation check."""
    monkeypatch.setattr(settings.agent, "prompt_cross_machine_delegation_enabled", True)

    from agent.graph._system_prompt import build_system_prompt

    prompt = build_system_prompt()

    assert _CROSS_MACHINE_DELEGATION_SENTENCE in prompt
    assert prompt.index(_CROSS_MACHINE_DELEGATION_SENTENCE) > prompt.index("# Before you act")


def test_cross_machine_delegation_hint_absent_from_full_prompt_when_off(
    monkeypatch: pytest.MonkeyPatch,
):
    """End-to-end: with the toggle off, the sentence is gone from the
    assembled prompt entirely."""
    monkeypatch.setattr(settings.agent, "prompt_cross_machine_delegation_enabled", False)

    from agent.graph._system_prompt import build_system_prompt

    prompt = build_system_prompt()

    assert _CROSS_MACHINE_DELEGATION_SENTENCE not in prompt


# ── activation telemetry (issue #40) ────────────────────────────────────────


def test_plugin_prompt_section_records_an_activation(monkeypatch: pytest.MonkeyPatch):
    """A plugin section that rendered text is prompt real estate the plugin is
    spending — recorded with its length + digest, so which variant landed is
    identifiable without storing the text. A section returning "" contributed
    nothing and records nothing."""
    from agent.graph import _system_prompt
    from shared import plugin_activation
    from shared.plugin_context import PluginContext

    recorded: list[tuple[str, str, str, str]] = []

    def spy(plugin: str | None, surface: str, identifier: str, *, detail: str = "") -> None:
        if plugin is not None:
            recorded.append((plugin, surface, identifier, detail))

    monkeypatch.setattr(plugin_activation, "record", spy)

    def loud_section() -> str:
        return "## Loud\n\nsomething."

    def silent_section() -> str:
        return ""

    saved = list(_system_prompt._SYSTEM_PROMPT_SECTIONS)
    try:
        # Isolate the build: touching the `ava` namespace earlier in the process
        # (any test that hits an `ava.*` miss) loads the builtin plugins, whose
        # sections are registered WITH plugin identity — their records would
        # land in `recorded` and this exact-list assertion would fail depending
        # on what ran before. Build with only the two sections under test.
        _system_prompt._SYSTEM_PROMPT_SECTIONS[:] = []
        with PluginContext("myplugin"):
            _system_prompt.register_system_prompt_section(loud_section)
            _system_prompt.register_system_prompt_section(silent_section)
        _system_prompt.build_system_prompt()
    finally:
        _system_prompt._SYSTEM_PROMPT_SECTIONS[:] = saved

    assert [(p, s, i) for p, s, i, _d in recorded] == [
        ("myplugin", "systemPromptSections", "loud_section")
    ]
    assert recorded[0][3].startswith(f"chars={len('## Loud\n\nsomething.')} sha=")
