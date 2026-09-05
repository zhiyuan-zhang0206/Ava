"""Model registry value types and shared tuning defaults."""

from __future__ import annotations

from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# ModelTuning — per-model DEFAULTS for settings fields
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ModelTuning:
    """Per-model defaults for per-model-defaultable settings fields.

    Field names MATCH the flat config field names (``shared.config``) exactly —
    ``resolve_setting`` maps between the two by name, and
    ``tests/shared/test_model_registry.py`` asserts the alignment. ``None``
    means "no per-model opinion; fall through to ``DEFAULT_TUNING``".

    Communication-style / prompt-section values are the per-model *behavior
    profile*: which guidance sections a model family gets and how chatty it
    should be. Mechanical values (compact fractions, retry, stream timeouts)
    tune the runtime around each model's failure modes.
    """

    # -- mechanical --
    auto_compact_fraction: float | None = None
    auto_compact_ceiling_tokens: int | None = None
    compact_reminder_fraction: float | None = None
    reasoning_effort: str | None = None
    claude_thinking_budget_tokens: int | None = None
    llm_retry_max_attempts: int | None = None
    llm_stream_ttft_timeout_seconds: float | None = None
    llm_stream_total_timeout_seconds: float | None = None
    llm_stream_inter_chunk_timeout_seconds: float | None = None
    # -- prompt behavior --
    agent_communication_style: str | None = None
    prompt_user_tone_enabled: bool | None = None
    prompt_prefer_sdk_enabled: bool | None = None
    prompt_keep_it_simple_enabled: bool | None = None
    prompt_output_conciseness_enabled: bool | None = None
    prompt_ui_delivery_enabled: bool | None = None
    prompt_outcome_reporting_enabled: bool | None = None
    prompt_action_caution_enabled: bool | None = None
    prompt_align_before_action_enabled: bool | None = None
    prompt_delegation_check_enabled: bool | None = None
    prompt_capabilities_match_first_enabled: bool | None = None
    prompt_cross_machine_delegation_enabled: bool | None = None
    prompt_file_driven_work_enabled: bool | None = None
    prompt_temporal_awareness_enabled: bool | None = None
    prompt_invest_future_enabled: bool | None = None
    prompt_memory_behavior_enabled: bool | None = None


# The shared-default floor: the values every model gets when neither the model
# entry nor the user says otherwise. These are the former pydantic Field
# defaults of the corresponding settings fields (moved here when those fields
# became None-sentinel). Every field must be non-None — asserted at import.
DEFAULT_TUNING = ModelTuning(
    # One flat rule for the whole roster: force-compact at 40% of the model's own
    # context window, remind at 30%. Both are fractions of `context_window`, so
    # "the same rule" means different absolute token counts per model, and a model
    # added to the registry inherits it with no entry of its own. The deepseek
    # entries are the roster's exception: user decision (2026-08-29) pins them at
    # soft 374k / hard 512k (0.374 / 0.512 of their 1M window).
    auto_compact_fraction=0.4,
    auto_compact_ceiling_tokens=0,  # 0 = no absolute cap; the fraction alone decides
    compact_reminder_fraction=0.3,
    # "" = "no per-model opinion" — the floor for models WITHOUT a pinned
    # tuning value. Spawnable models must pin a concrete value (validated
    # below): the spawn picker pre-selects each model's default effort, and ""
    # is not displayable as a default. An explicit AVA_REASONING_EFFORT=""
    # still pins "provider's own default" at the config layer.
    reasoning_effort="",  # "" = each provider's own default effort
    claude_thinking_budget_tokens=0,  # 0 = leave extended thinking off
    llm_retry_max_attempts=6,
    llm_stream_ttft_timeout_seconds=30.0,
    # Hard wall for one streaming attempt. Gap timeouts still catch silent
    # providers sooner; this ceiling catches a response that drip-feeds forever.
    llm_stream_total_timeout_seconds=3600.0,
    # 300, not 10: Claude Code (API_FORCE_IDLE_TIMEOUT, documented as a 5-minute
    # idle timeout) and Codex CLI (stream_idle_timeout_ms) independently ship 300
    # for this exact parameter. Long mid-stream silence is documented-normal, not
    # a fault — Anthropic's default `display: "omitted"` thinking emits NO
    # thinking_delta at all, and the streaming docs warn of tool-call gaps. At 10
    # this timeout was manufacturing turn aborts out of healthy streams.
    llm_stream_inter_chunk_timeout_seconds=300.0,
    # User ruling (2026-08-22): narration guidance is off by default for every
    # model; the section is omitted unless an explicit value opts in.
    agent_communication_style="off",
    # User decision (2026-09-03): tone guidance is on by default; Claude models opt out.
    prompt_user_tone_enabled=True,
    prompt_prefer_sdk_enabled=True,
    prompt_keep_it_simple_enabled=True,
    prompt_output_conciseness_enabled=True,
    prompt_ui_delivery_enabled=True,
    prompt_outcome_reporting_enabled=True,
    prompt_action_caution_enabled=True,
    prompt_align_before_action_enabled=True,
    prompt_delegation_check_enabled=True,
    prompt_capabilities_match_first_enabled=True,
    prompt_cross_machine_delegation_enabled=True,
    prompt_file_driven_work_enabled=True,
    prompt_temporal_awareness_enabled=True,
    prompt_invest_future_enabled=True,
    prompt_memory_behavior_enabled=True,
)


# ---------------------------------------------------------------------------
# ModelSpec — per-model facts + the tuning defaults
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ModelSpec:
    """Everything the framework knows about one concrete model id.

    Facts (windows, caps, effort vocabulary) drive the factory, the
    compact machinery, and the UI catalog; ``tuning`` carries the per-model
    settings defaults resolved by ``resolve_setting``. A fact left ``None``
    means "unknown / not applicable" and keeps the model out of the
    corresponding derived view — exactly the membership the retired parallel
    tables had.
    """

    provider: str  # SUPPORTED_MODELS group key == build_chat_model prefix
    spawnable: bool = False  # offered in the frontend spawn dropdown
    unavailable_fallback: str | None = None  # temporarily withdraw this id from new selections
    # while preserving existing configurations: build_chat_model resolves the
    # explicitly named fallback. The target must be a registered, spawnable
    # model; no provider error is silently retried as a fallback.
    superseded_by: str | None = None  # the model id that replaced this one in the
    # spawn picker. Purely a display fact: a superseded model stays spawnable
    # (and therefore fully config-valid — spawn/restart validation and the
    # settings panels accept it unchanged), the picker just hides it by default
    # in favor of the replacement. The replacement chain is set explicitly at
    # onboarding time (see the model-switch playbook), never inferred at runtime.
    context_window: int | None = None  # max input tokens (compact thresholds derive from it)
    max_output_tokens: int | None = None  # documented output cap; pinned explicitly on the
    # anthropic-protocol branches (claude/deepseek) — langchain-anthropic falls back to a
    # legacy 4096 default for unknown ids, truncating thinking mid-turn (#169)
    knowledge_cutoff: str | None = None  # YYYY-MM, appended to the system prompt
    model_identity: str | None = (
        None  # per-model identity note, injected before cutoff in system prompt
    )
    effort_levels: tuple[str, ...] | None = None  # the effort vocabulary this model's knob
    # accepts (wire `output_config.effort` levels for adaptive claude; the binary
    # thinking on/off vocabulary for extended-thinking-only models; the provider's
    # clamp vocabulary elsewhere). Serves the spawn-dialog dropdown + the claude clamp.
    extended_thinking_only: bool = False  # claude models whose only thinking mode is manual
    # extended thinking (budget_tokens; default OFF) and that 400 on `effort`
    thinking_always_on: bool = False  # models whose reasoning cannot be switched off: a
    # caller disabling thinking gets a warning instead of a wire body the endpoint
    # rejects (kimi-k3; glm-5.3 / glm-5.3-flash, whose thinking.type=disabled 400s —
    # verified live 2026-08-27, error code 1210)
    streaming: bool = True  # construction-time streaming default; False only where a
    # model's streaming path is known-worse than its non-streaming one
    media_types: frozenset[str] = frozenset()
    """Media types the model's provider binding accepts natively. Members are
    "image" / "pdf" / "audio" / "video". Empty (default) = text-only. Supersedes
    the per-model `vision` bool (Task #1342): one field carries the whole capability
    matrix, and `model_supports_vision` derives from it."""
    attach_modalities: frozenset[str] | None = None
    """The media types `ava.self.attach` accepts for this model, when attach is
    more restrictive than the model's native `media_types` (user ruling
    2026-08-28). Attach registers local files into the same message pipeline,
    so the native matrix is the default contract — None means "no attach-specific
    opinion; follow `media_types`". Empty frozenset = attach is unavailable even
    though the endpoint could receive media. A declared set must be a subset of
    `media_types` (enforced by `_validate_spec`): a model cannot attach a
    modality its endpoint cannot receive."""
    tuning: ModelTuning = field(default_factory=ModelTuning)


# Effort vocabularies shared by every model of a family — named once so the
# entries below stay readable; still per-model data (a model may diverge, e.g.
# claude-sonnet-4-6 has no xhigh).
_CLAUDE_ADAPTIVE_EFFORT = ("low", "medium", "high", "xhigh", "max")
_GPT_EFFORT = ("none", "low", "medium", "high", "xhigh", "max")

# These classes remain public through shared.lm.registry; preserve their
# historical identity for introspection and pickle compatibility.
ModelTuning.__module__ = "shared.lm.registry"
ModelSpec.__module__ = "shared.lm.registry"
