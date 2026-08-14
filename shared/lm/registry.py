"""Single per-model registry — every per-model fact and per-model tunable
default lives in one ``MODELS: dict[id, ModelSpec]`` table.

Replaces the parallel per-model-id tables that had accumulated across
``factory.py`` / ``_effort.py`` / ``pricing.py`` (``SUPPORTED_MODELS``,
``MODEL_CONTEXT_WINDOW``, ``MODEL_KNOWLEDGE_CUTOFF``, ``_MODEL_DEFAULT_STREAMING``,
``_CLAUDE_MAX_TOKENS``, ``_DEEPSEEK_MAX_TOKENS``, ``_CLAUDE_EFFORT_LEVELS``,
``_CLAUDE_EXTENDED_THINKING_ONLY``, ``_CLAUDE_EXTENDED_THINKING_EFFORT_LEVELS``,
``MODEL_PRICING``) — their membership had drifted apart because adding a model
meant editing up to a dozen dicts. Here a model is one entry; the legacy table
names survive as derived views (below) so existing import sites keep working.
Per-PROVIDER tables (prefix → API key, wire effort vocabularies for the
OpenAI-style endpoints, vision prefixes) are *not* per-model facts and stay in
``factory.py`` / ``_effort.py``.

## Config layering — how a per-model default takes effect

Everything tunable is per-model by default, with a shared fallback::

    code shared default            (DEFAULT_TUNING — the fully-populated floor)
    < per-model default            (MODELS[id].tuning — code table, None = no opinion)
    < .env / env explicit value    (the user's deliberate global choice)
    < per-agent overlay            (spawn/restart config_overlay via set_field)

``resolve_setting`` implements the layering. The sentinel for "the user did not
explicitly choose" is the settings field's ``None`` default: per-model-defaultable
settings fields are typed ``T | None = None``, and their former pydantic defaults
moved into ``DEFAULT_TUNING`` here. A non-None settings value always means an
explicit choice — whether it came from ``.env``, an exported env var, a gateway
bootstrap payload that forwards a ``.env``-set value, or a per-agent overlay
(``set_field`` writes a non-None value onto the settings singleton).

Why not detect explicitness via ``model_fields_set``: a split agent-runner
receives its config injected into ``os.environ`` from the gateway's
``/api/bootstrap``, which serves *every* bootstrap field (defaults included) —
``model_fields_set`` would mark everything explicit on a runner but not on a
single box, making the layering topology-dependent. The None-sentinel travels
as data through every distribution path (an unset field serializes as absent,
``bootstrap_config_values`` skips None), so the layering is uniform.

Per-model values are a CODE table on purpose — not a config dimension. The
``model_overrides`` override-store shape was retired by migration 0047; the
"one value lives in exactly one place" invariant holds: a per-model default is
code, an explicit user choice is ``.env``, a per-agent choice is the overlay.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from dataclasses import fields as dataclass_fields
from typing import Any

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
    llm_stream_inter_chunk_timeout_seconds: float | None = None
    # -- prompt behavior --
    agent_communication_style: str | None = None
    prompt_prefer_sdk_enabled: bool | None = None
    prompt_output_conciseness_enabled: bool | None = None
    prompt_ui_delivery_enabled: bool | None = None
    prompt_outcome_reporting_enabled: bool | None = None
    prompt_action_caution_enabled: bool | None = None
    prompt_align_before_action_enabled: bool | None = None
    prompt_delegation_check_enabled: bool | None = None
    prompt_cross_machine_delegation_enabled: bool | None = None
    prompt_file_driven_work_enabled: bool | None = None
    prompt_temporal_awareness_enabled: bool | None = None
    prompt_memory_behavior_enabled: bool | None = None
    agent_reflection_enabled: bool | None = None


# The shared-default floor: the values every model gets when neither the model
# entry nor the user says otherwise. These are the former pydantic Field
# defaults of the corresponding settings fields (moved here when those fields
# became None-sentinel). Every field must be non-None — asserted at import.
DEFAULT_TUNING = ModelTuning(
    # One flat rule for the whole roster: force-compact at 40% of the model's own
    # context window, remind at 30%. Both are fractions of `context_window`, so
    # "the same rule" means different absolute token counts per model, and a model
    # added to the registry inherits it with no entry of its own. The deepseek
    # entries are the roster's exception: user decision (task #581) pins them at
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
    # 300, not 10: Claude Code (API_FORCE_IDLE_TIMEOUT, documented as a 5-minute
    # idle timeout) and Codex CLI (stream_idle_timeout_ms) independently ship 300
    # for this exact parameter. Long mid-stream silence is documented-normal, not
    # a fault — Anthropic's default `display: "omitted"` thinking emits NO
    # thinking_delta at all, and the streaming docs warn of tool-call gaps. At 10
    # this timeout was manufacturing turn aborts out of healthy streams.
    llm_stream_inter_chunk_timeout_seconds=300.0,
    agent_communication_style="oriented",
    prompt_prefer_sdk_enabled=True,
    prompt_output_conciseness_enabled=True,
    prompt_ui_delivery_enabled=True,
    prompt_outcome_reporting_enabled=True,
    prompt_action_caution_enabled=True,
    prompt_align_before_action_enabled=True,
    prompt_delegation_check_enabled=True,
    prompt_cross_machine_delegation_enabled=True,
    prompt_file_driven_work_enabled=True,
    prompt_temporal_awareness_enabled=True,
    prompt_memory_behavior_enabled=True,
    agent_reflection_enabled=True,
)


# ---------------------------------------------------------------------------
# ModelSpec — per-model facts + the tuning defaults
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ModelSpec:
    """Everything the framework knows about one concrete model id.

    Facts (windows, caps, pricing, effort vocabulary) drive the factory, the
    compact machinery, and the UI catalog; ``tuning`` carries the per-model
    settings defaults resolved by ``resolve_setting``. A fact left ``None``
    means "unknown / not applicable" and keeps the model out of the
    corresponding derived view — exactly the membership the retired parallel
    tables had.
    """

    provider: str  # SUPPORTED_MODELS group key == build_chat_model prefix
    spawnable: bool = False  # offered in the frontend spawn dropdown
    context_window: int | None = None  # max input tokens (compact thresholds derive from it)
    max_output_tokens: int | None = None  # documented output cap; pinned explicitly on the
    # anthropic-protocol branches (claude/deepseek) — langchain-anthropic falls back to a
    # legacy 4096 default for unknown ids, truncating thinking mid-turn (#169)
    knowledge_cutoff: str | None = None  # YYYY-MM, appended to the system prompt
    model_identity: str | None = (
        None  # per-model identity note, injected before cutoff in system prompt
    )
    pricing: tuple[float, float, float] | None = None  # (cache_miss, cache_hit, out) USD/M —
    # 3-tuple because cache-hit rates differ from miss by up to 120x (see pricing.py)
    effort_levels: tuple[str, ...] | None = None  # the effort vocabulary this model's knob
    # accepts (wire `output_config.effort` levels for adaptive claude; the binary
    # thinking on/off vocabulary for extended-thinking-only models; the provider's
    # clamp vocabulary elsewhere). Serves the spawn-dialog dropdown + the claude clamp.
    extended_thinking_only: bool = False  # claude models whose only thinking mode is manual
    # extended thinking (budget_tokens; default OFF) and that 400 on `effort`
    streaming: bool = True  # construction-time streaming default; False only where a
    # model's streaming path is known-worse than its non-streaming one
    tuning: ModelTuning = field(default_factory=ModelTuning)


# Effort vocabularies shared by every model of a family — named once so the
# entries below stay readable; still per-model data (a model may diverge, e.g.
# claude-sonnet-4-6 has no xhigh).
_CLAUDE_ADAPTIVE_EFFORT = ("low", "medium", "high", "xhigh", "max")
_GPT_EFFORT = ("none", "low", "medium", "high", "xhigh", "max")

MODELS: dict[str, ModelSpec] = {
    # -- deepseek --
    "deepseek-v4-pro": ModelSpec(
        provider="deepseek",
        spawnable=True,
        context_window=1_000_000,
        max_output_tokens=384_000,
        knowledge_cutoff="2026-04",
        model_identity="You are running on DeepSeek V4 Pro.",
        # Standing post-promo price (75% launch promo expired 2026-05-31);
        # real cache discount (~120x cheaper on hit).
        pricing=(0.435, 0.003625, 0.87),
        effort_levels=("high", "max"),
        tuning=ModelTuning(
            # DeepSeek defaults to `high` and only auto-promotes to `max` for
            # harnesses it recognizes (it names Claude Code / OpenCode); Ava is
            # not on that list, so the promotion has to be explicit. Their own
            # V4 report has max beating high on EVERY agentic metric.
            reasoning_effort="max",
            # DeepSeek documents queueing a request for up to 10 minutes while
            # emitting only SSE COMMENT frames (`: keep-alive`), which no SDK
            # surfaces as a chunk — so a healthy queued request is indistinguish-
            # able from a dead one until data starts. 600 matches the server's
            # own connection-close cutoff.
            llm_stream_ttft_timeout_seconds=600.0,
            # Compact thresholds pinned 2026-08-01 (user decision, task #581):
            # soft 374k / hard 512k. The flat 30/40 rule's 400k hard threshold
            # sat below DeepSeek's own published trigger (512K), compacting
            # 1M-window agents a quarter-million tokens before the vendor does.
            # 0.512 / 0.374 of the 1M window is exactly 512_000 / 374_000
            # (decisions/2026-08-01-deepseek-compact-thresholds.md).
            auto_compact_fraction=0.512,
            compact_reminder_fraction=0.374,
        ),
    ),
    "deepseek-v4-flash": ModelSpec(
        provider="deepseek",
        spawnable=True,
        context_window=1_000_000,
        max_output_tokens=384_000,
        knowledge_cutoff="2026-04",
        model_identity="You are running on DeepSeek V4 Flash.",
        pricing=(0.14, 0.0028, 0.28),  # cache hit 50x discount (api-docs.deepseek.com)
        effort_levels=("high", "max"),
        tuning=ModelTuning(
            reasoning_effort="max",  # same as pro: Ava is not an auto-promoted harness
            llm_stream_ttft_timeout_seconds=600.0,  # same documented 10-minute queue
            # Same user decision as deepseek-v4-pro (task #581): soft 374k /
            # hard 512k on the 1M window — 0.512 / 0.374 exactly.
            auto_compact_fraction=0.512,
            compact_reminder_fraction=0.374,
        ),
    ),
    # -- claude --
    "claude-sonnet-5": ModelSpec(
        provider="claude",
        spawnable=True,
        context_window=1_000_000,
        max_output_tokens=128_000,
        knowledge_cutoff="2026-01",
        # Sonnet 5 intro pricing ($2/$10) through Aug 31, 2026, then $3/$15 standard.
        pricing=(2.0, 0.20, 10.0),  # cache read 0.20/M; re-check after Aug 31
        effort_levels=_CLAUDE_ADAPTIVE_EFFORT,
        tuning=ModelTuning(
            # Pinned 2026-08-01 (user decision, task #568): the picker must
            # show a concrete default, and Anthropic documents `high` as this
            # family's default effort (decisions/2026-07-25-per-model-
            # tuning-values.md Decision 4; "" is exactly equivalent to omitting
            # the parameter, whose default is high). NOT the ladder floor.
            reasoning_effort="high",
        ),
    ),
    "claude-haiku-4-5-20251001": ModelSpec(
        provider="claude",
        spawnable=True,
        context_window=200_000,
        max_output_tokens=64_000,
        knowledge_cutoff="2025-10",
        pricing=(1.0, 0.10, 5.0),  # cache read 0.10/M (10x vs input)
        # No wire `effort` field (server 400) — the cross-provider knob clamps
        # onto the manual-thinking on/off binary instead.
        effort_levels=("none", "high"),
        extended_thinking_only=True,
        tuning=ModelTuning(
            # Manual extended thinking defaults OFF on this model (budget_tokens
            # default 0) — the honest concrete default is the off rung.
            reasoning_effort="none",
        ),
    ),
    "claude-opus-5": ModelSpec(
        provider="claude",
        spawnable=True,
        context_window=1_000_000,
        max_output_tokens=128_000,
        knowledge_cutoff="2026-01",
        pricing=(5.0, 0.50, 25.0),  # cache read 0.50/M (10x vs input)
        effort_levels=_CLAUDE_ADAPTIVE_EFFORT,
        tuning=ModelTuning(
            # Pinned 2026-08-01 (task #568): Anthropic documents `high` as the
            # family default (see claude-sonnet-5).
            reasoning_effort="high",
        ),
    ),
    "claude-fable-5": ModelSpec(
        provider="claude",
        spawnable=True,
        context_window=1_000_000,
        max_output_tokens=128_000,
        knowledge_cutoff="2026-01",
        # Fable 5 — Mythos-class frontier model, 2x Opus cost.
        pricing=(10.0, 1.0, 50.0),  # cache read 1.00/M
        effort_levels=_CLAUDE_ADAPTIVE_EFFORT,
        tuning=ModelTuning(
            # Pinned 2026-08-01 (task #568): Anthropic documents `high` as the
            # family default (see claude-sonnet-5).
            reasoning_effort="high",
            # Fable 5 is the roster's only structurally throughput-limited
            # model: 25-40% of every other model's ITPM/OTPM at every tier, plus
            # 2.5x fewer RPM above the Start tier, while costing 2x Opus and
            # running the longest turns. More 429s means more provider-SDK
            # internal retries, which are silent on the wire.
            llm_retry_max_attempts=10,
            # That same silence is what TTFT measures: while the SDK retries a
            # 429 internally the socket produces no event at all, so a throttled
            # (but healthy) request looks identical to a hung one at 30s.
            llm_stream_ttft_timeout_seconds=120.0,
        ),
    ),
    # -- gemini --
    "gemini-3.7-flash": ModelSpec(
        provider="gemini",
        spawnable=True,
        context_window=1_048_576,
        # Google does not publish a cutoff for 3.7 Flash; carries the 3.6
        # estimate forward (3.6 GA'd 2026-07-21, 3.7 GA'd 2026-08-13).
        knowledge_cutoff="2026-03",
        # Intro pricing ($0.75/$3.75) through 2026-12-31, then the standard
        # $1.50/$7.50 — same standard rate as 3.6. Cache read ~0.1x input.
        pricing=(0.75, 0.075, 3.75),
        effort_levels=("minimal", "low", "medium", "high"),  # thinking_level vocabulary
        tuning=ModelTuning(
            # Pinned 2026-08-01 (task #568): Gemini flash default thinking_level
            # is `medium` (decisions/2026-07-25-per-model-tuning-values.md).
            reasoning_effort="medium",
        ),
    ),
    "gemini-3.5-flash": ModelSpec(
        provider="gemini",
        spawnable=True,
        context_window=1_048_576,
        knowledge_cutoff="2025-01",
        pricing=(1.5, 0.15, 9.0),
        effort_levels=("minimal", "low", "medium", "high"),
        tuning=ModelTuning(
            # Pinned 2026-08-01 (task #568): flash default thinking_level is
            # `medium` (see gemini-3.7-flash).
            reasoning_effort="medium",
        ),
    ),
    "gemini-3.1-pro-preview": ModelSpec(
        provider="gemini",
        spawnable=True,
        # 1,048,576 per three independent first-party Google pages (the model
        # page, the thinking guide, and the DeepMind model card). Every "2M"
        # claim traces back to speculative blogs about a rumored Ultra tier.
        context_window=1_048_576,
        knowledge_cutoff="2025-01",
        # Also has a >200K-context tier (4.0 / 0.4 / 18.0); the flat tuple
        # encodes the <=200K base tier, so large-context calls are under-priced.
        pricing=(2.0, 0.2, 12.0),
        effort_levels=("minimal", "low", "medium", "high"),
        tuning=ModelTuning(
            # Pinned 2026-08-01 (task #568): this model defaults to
            # thinking_level=high and cannot drop to minimal (Google docs;
            # also recorded in decisions/2026-07-25-per-model-tuning-
            # values.md Decision 3).
            reasoning_effort="high",
            # The only asymmetry Google's own docs admit inside this family:
            # preview models "might come with more restrictive rate limits" and
            # "rate limits are more restricted for experimental and preview
            # models"; the two flashes are GA. Matching first-party 503 reports
            # against paid accounts since launch.
            llm_retry_max_attempts=10,
            # Gemini's wire has no protocol preamble (no message_start /
            # response.created), so the first chunk IS the first content or
            # thought — unlike Claude and GPT, thinking time lands inside TTFT
            # here. This model defaults to thinking_level=high and cannot go to
            # `minimal`, and first-output latency of 17s+ was observed during a
            # Vertex degradation.
            llm_stream_ttft_timeout_seconds=90.0,
        ),
    ),
    # -- gpt --
    "gpt-5.6-sol": ModelSpec(
        provider="gpt",
        spawnable=True,
        context_window=1_050_000,
        knowledge_cutoff="2026-02",
        # Flagship tier (explicit id; the bare gpt-5.6 alias also routes here,
        # but the catalog pins the explicit tier id like terra/luna).
        pricing=(5.0, 0.5, 30.0),  # cached-input discount ~0.1x
        effort_levels=_GPT_EFFORT,
        tuning=ModelTuning(
            # Pinned 2026-08-01 (task #568): OpenAI documents `medium` as the
            # default reasoning effort (decisions/2026-07-25-per-model-
            # tuning-values.md Decision 4).
            reasoning_effort="medium",
        ),
    ),
    "gpt-5.6-terra": ModelSpec(
        provider="gpt",
        spawnable=True,
        context_window=1_050_000,
        knowledge_cutoff="2026-02",
        # Balanced tier. 2026-07-30 OpenAI cut Terra 20% (was 2.50/0.25/15.0
        # at the 2026-07-09 launch) — verified against the live model card
        # (developers.openai.com/api/docs/models/gpt-5.6-terra); Sol's price
        # is unchanged by this cut.
        pricing=(2.0, 0.20, 12.0),
        effort_levels=_GPT_EFFORT,
        # Same window, same effort ladder across all three tiers — OpenAI
        # documents no per-tier difference in anything Ava tunes.
        tuning=ModelTuning(reasoning_effort="medium"),  # OpenAI default (see gpt-5.6-sol)
    ),
    "gpt-5.6-luna": ModelSpec(
        provider="gpt",
        spawnable=True,
        context_window=1_050_000,
        knowledge_cutoff="2026-02",
        # Cost-optimized tier. 2026-07-30 OpenAI cut Luna 80% (was
        # 1.0/0.10/6.0 at the 2026-07-09 launch) — verified against the live
        # model card (developers.openai.com/api/docs/models/gpt-5.6-luna).
        pricing=(0.20, 0.02, 1.20),
        effort_levels=_GPT_EFFORT,
        tuning=ModelTuning(reasoning_effort="medium"),  # OpenAI default (see gpt-5.6-sol)
    ),
    # -- mimo --
    "mimo-v2.5-pro": ModelSpec(
        provider="mimo",
        spawnable=True,
        # Xiaomi's model page and the HF card both say 1M context / 128K max
        # output — the old 128,000 window was the OUTPUT cap filed as the
        # window, which understated the window ~8x and made the compact
        # threshold unreachable in practice.
        context_window=1_000_000,
        max_output_tokens=128_000,
        knowledge_cutoff="2024-12",
        # Benchmarked to the DeepSeek V4 Pro tier (3 / 0.025 / 6 RMB/M).
        pricing=(0.435, 0.0036, 0.87),
        effort_levels=("none", "high"),  # body-level thinking on/off only
        tuning=ModelTuning(
            # Pinned 2026-08-01 (task #568): the "high" rung IS the provider
            # default — thinking is on unless explicitly disabled
            # (shared/lm/_effort.py:mimo_extra_body).
            reasoning_effort="high",
        ),
    ),
    "mimo-v2.5-pro-ultraspeed": ModelSpec(
        provider="mimo",
        spawnable=True,
        # Same 1T/42B weights as Pro, served on the TileRT stack — same window
        # and output cap; only throughput and price differ.
        context_window=1_000_000,
        max_output_tokens=128_000,
        knowledge_cutoff="2024-12",
        # ultraspeed = 3x standard across all tiers. Still application-gated
        # beta: the 2026-06-09..06-23 trial window was extended indefinitely
        # (end date "to be announced"), so this is the standing rate, not a
        # trial rate.
        pricing=(1.305, 0.0108, 2.61),
        effort_levels=("none", "high"),
        tuning=ModelTuning(
            # Pinned 2026-08-01 (task #568): "high" = provider default (see
            # mimo-v2.5-pro).
            reasoning_effort="high",
            # Xiaomi omits this variant from the published RPM/TPM table
            # entirely and gates it behind an application ("limited slots"),
            # i.e. its serving capacity is self-declared scarce.
            llm_retry_max_attempts=10,
        ),
    ),
    # -- kimi --
    "kimi-k3": ModelSpec(
        provider="kimi",
        spawnable=True,
        context_window=1_048_576,
        knowledge_cutoff="2025-12",
        model_identity="You are running on Kimi K3 (Moonshot).",
        # Official pricing: https://platform.moonshot.ai/docs/pricing
        # (cache miss / cache hit / output, USD per 1M tokens)
        pricing=(3.0, 0.30, 15.0),
        effort_levels=("low", "high", "max"),
        # Streams (the registry default). The former streaming=False carried
        # "streaming returns ~40% 429" — no incident record backs the asymmetry,
        # and Moonshot's own troubleshooting page recommends stream=True
        # precisely to avoid connection errors (non-streaming makes the server
        # withhold the response header until generation completes). The
        # asymmetry is more likely an artifact of the timeouts this commit
        # fixes: the streaming path was killed by a 30s TTFT while the
        # non-streaming fallback got 600s. See
        # decisions/2026-07-25-per-model-tuning-values.md.
        tuning=ModelTuning(
            # Pinned 2026-08-01 (task #568): Moonshot documents K3's default
            # effort as `max` (decisions/2026-07-25-per-model-tuning-
            # values.md Decision 4).
            reasoning_effort="max",
            # Moonshot's rate limits are account-wide across every key AND every
            # model, capacity for K3 was scarce enough that new subscriptions
            # were paused, and engine_overloaded_error is explicitly defined as
            # server-side capacity that upgrading your tier does not fix.
            llm_retry_max_attempts=10,
            # The recorded K3 failure (PR #496): the provider SDK retries a 429
            # internally, gets a 200, but the overloaded engine never starts
            # streaming — no bytes, so 30s TTFT fired before the retry could land.
            llm_stream_ttft_timeout_seconds=120.0,
        ),
    ),
    # -- glm --
    "glm-5.2": ModelSpec(
        provider="glm",
        spawnable=True,
        context_window=1_000_000,
        knowledge_cutoff="2025-12",
        # ¥8/M input, ¥28/M output (~$1.10/$3.86 USD at Jul 2026 rate).
        # Cache-hit discount: unverified; conservative 0.5x input placeholder.
        pricing=(1.10, 0.55, 3.86),
        effort_levels=("high", "max"),
        tuning=ModelTuning(
            # Pinned 2026-08-01 (task #568): Z.ai documents GLM-5.2's default
            # effort as `max` (decisions/2026-07-25-per-model-tuning-
            # values.md Decision 4).
            reasoning_effort="max",
            # The roster's best-documented overload history, from Z.ai's own
            # issue tracker: a single non-batch paid user logged 285 HTTP 429s in
            # one day (~50% of all requests) and a full hour at 100% failure.
            # Z.ai's own guidance for their 1305 code ("platform service overload") is to
            # lengthen the retry interval and avoid fixed-interval hammering.
            llm_retry_max_attempts=10,
        ),
    ),
    # -- grok --
    "grok-4.5": ModelSpec(
        provider="grok",
        spawnable=True,
        context_window=500_000,
        knowledge_cutoff="2026-02",
        pricing=(2.00, 0.50, 6.0),  # $2.00/$6.00 short-context, $0.50 cached input
        effort_levels=("low", "medium", "high"),
        tuning=ModelTuning(
            # Pinned 2026-08-01 (task #568): xAI documents `high` as the
            # default (decisions/2026-07-25-per-model-tuning-values.md).
            reasoning_effort="high",
        ),
    ),
    # -- legacy / non-spawnable (facts kept for cost attribution + old agents) --
    "claude-opus-4-8": ModelSpec(
        provider="claude",
        context_window=200_000,
        max_output_tokens=128_000,
        knowledge_cutoff="2026-01",
        pricing=(5.0, 0.50, 25.0),
        effort_levels=_CLAUDE_ADAPTIVE_EFFORT,
    ),
    "claude-sonnet-4-6": ModelSpec(
        provider="claude",
        context_window=200_000,
        max_output_tokens=128_000,
        knowledge_cutoff="2025-08",
        pricing=(3.0, 0.30, 15.0),
        effort_levels=("low", "medium", "high", "max"),  # xhigh arrived with opus-4-7
    ),
    "claude-opus-4-7": ModelSpec(
        provider="claude",
        max_output_tokens=128_000,
        pricing=(5.0, 0.50, 25.0),
        effort_levels=_CLAUDE_ADAPTIVE_EFFORT,
    ),
    "claude-opus-4-6": ModelSpec(
        provider="claude",
        pricing=(5.0, 0.50, 25.0),
    ),
    # Bare alias of the dated snapshot above — pricing-only so historical cost
    # lookups keyed by the dateless convenience id still resolve. Carries the
    # same extended-thinking-only flag as the dated entry: without it the
    # factory's adaptive-thinking default would send `type: "adaptive"`, which
    # this model 400s on.
    "claude-haiku-4-5": ModelSpec(
        provider="claude",
        pricing=(1.0, 0.10, 5.0),
        extended_thinking_only=True,
    ),
    "gemini-2.5-pro": ModelSpec(
        provider="gemini",
        pricing=(1.25, 0.125, 10.0),  # ≤200K standard tier (ai.google.dev/pricing)
    ),
    "gemini-2.5-flash": ModelSpec(
        provider="gemini",
        pricing=(0.30, 0.03, 2.50),  # ≤200K standard tier
    ),
    "gpt-5.5": ModelSpec(
        provider="gpt",
        context_window=256_000,
        knowledge_cutoff="2025-12",
        pricing=(5.0, 0.5, 30.0),
    ),
    "gpt-5.4-mini": ModelSpec(
        provider="gpt",
        context_window=256_000,
        knowledge_cutoff="2025-08",
        pricing=(0.75, 0.075, 4.5),
    ),
}


# ---------------------------------------------------------------------------
# Derived views — the legacy table names, computed from MODELS
# ---------------------------------------------------------------------------

# Models offered in the frontend spawn dropdown + per-agent overlay, grouped by
# provider in registry order. Adding a model to MODELS with spawnable=True is
# the only edit needed for it to appear in the UI; provider availability still
# depends on the corresponding API key being set on the agent-runner.
SUPPORTED_MODELS: dict[str, list[str]] = {}
for _model_id, _spec in MODELS.items():
    if _spec.spawnable:
        SUPPORTED_MODELS.setdefault(_spec.provider, []).append(_model_id)

# Model context window sizes (max input tokens). Used by the token-usage
# endpoint and the compact machinery; a model without a known window is absent
# (the frontend hides the max segment).
MODEL_CONTEXT_WINDOW: dict[str, int] = {
    _model_id: _spec.context_window
    for _model_id, _spec in MODELS.items()
    if _spec.context_window is not None
}

# Knowledge cutoff dates (YYYY-MM), appended to the system prompt so the agent
# knows the temporal boundary of its training data. A model without one simply
# gets no cutoff line.
MODEL_KNOWLEDGE_CUTOFF: dict[str, str] = {
    _model_id: _spec.knowledge_cutoff
    for _model_id, _spec in MODELS.items()
    if _spec.knowledge_cutoff is not None
}

# Model identity — a per-model note injected before the knowledge cutoff
# in the system prompt so the model knows what it is running on.
MODEL_IDENTITY: dict[str, str] = {
    _model_id: _spec.model_identity
    for _model_id, _spec in MODELS.items()
    if _spec.model_identity is not None
}


def _validate_registry() -> None:
    """Fail fast at import on a registry gap.

    A spawnable model missing a core fact would surface as a degraded UI row /
    an uncompactable agent / an unpriced eval — catch it where the entry is
    written instead.
    """
    for tuning_field in dataclass_fields(ModelTuning):
        if getattr(DEFAULT_TUNING, tuning_field.name) is None:
            raise RuntimeError(
                f"DEFAULT_TUNING.{tuning_field.name} is None — the shared-default floor "
                f"must be fully populated (it is the last resort of resolve_setting)"
            )
    for model_id, spec in MODELS.items():
        if not spec.spawnable:
            continue
        missing = [
            fact
            for fact in ("context_window", "knowledge_cutoff", "pricing", "effort_levels")
            if getattr(spec, fact) is None
        ]
        if missing:
            raise RuntimeError(
                f"spawnable model {model_id!r} is missing registry facts {missing} — "
                f"fill them in shared/lm/registry.py:MODELS"
            )
        # The spawn picker pre-selects each model's default effort
        # (GET /api/models reasoning_effort_default) — without a concrete
        # per-model value it cannot show one ("" means "provider's own
        # default", which is not a displayable rung), and the UI would regress
        # to a synthetic "Effort: default" option. Pin a real default (the
        # vendor's documented one; see decisions/2026-07-25-per-model-
        # tuning-values.md Decision 4).
        if not spec.tuning.reasoning_effort:
            raise RuntimeError(
                f"spawnable model {model_id!r} has no concrete reasoning_effort default — "
                f"pin one in its ModelTuning ('' provider-default is not displayable "
                f"in the spawn picker)"
            )
        if spec.provider in ("claude", "deepseek") and spec.max_output_tokens is None:
            raise RuntimeError(
                f"spawnable {spec.provider} model {model_id!r} needs max_output_tokens — "
                f"the anthropic-protocol branches must pin the output cap explicitly"
            )


_validate_registry()


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ResolvedSetting:
    """One setting's effective value plus the layer that produced it.

    The introspectable form of ``resolve_setting``: same layering, but it names
    the winning layer and keeps every candidate, so an operator can see WHY a
    model runs with the value it does instead of only what the value is.
    """

    setting: str
    value: Any  # the effective value — what resolve_setting returns
    source: str  # "explicit" | "model-default" | "shared-default"
    shared_default: Any  # the DEFAULT_TUNING floor; always present
    model_default: Any | None  # this model's own opinion (None = none / unregistered)
    explicit_value: Any | None  # the user's pinned value (None = not pinned)


def tuning_field_names() -> tuple[str, ...]:
    """Every per-model-defaultable settings field name, in ``ModelTuning`` order.

    The exact set of fields ``resolve_setting`` governs — what a per-model view
    enumerates, so adding a tunable to ``ModelTuning`` surfaces it with no
    second list to keep in sync.
    """
    return tuple(f.name for f in dataclass_fields(ModelTuning))


def explain_setting(setting: str, *, model: str, explicit: Any) -> ResolvedSetting:
    """``resolve_setting``'s layering, with the winning layer named.

    The explicit value is an ARGUMENT rather than read here: the runtime passes
    the in-process settings value, while the config panel passes the value read
    fresh from `.env` (what the next agent process will boot with). One layering
    implementation for both, so the displayed resolution cannot drift from the
    one the agent actually gets.
    """
    floor = getattr(DEFAULT_TUNING, setting)
    spec = MODELS.get(model)
    tuned = getattr(spec.tuning, setting) if spec is not None else None
    if explicit is not None:
        return ResolvedSetting(setting, explicit, "explicit", floor, tuned, explicit)
    if tuned is not None:
        return ResolvedSetting(setting, tuned, "model-default", floor, tuned, None)
    return ResolvedSetting(setting, floor, "shared-default", floor, tuned, None)


def resolve_setting(setting: str, *, model: str) -> Any:
    """The effective value of a per-model-defaultable settings field for `model`.

    Layering (weakest first): ``DEFAULT_TUNING`` shared default < the model's
    ``tuning`` entry < an explicit settings value. The settings value is
    explicit exactly when it is non-None — these fields are ``T | None = None``
    and every real source (.env, exported env, bootstrap-forwarded env, the
    per-agent config overlay) writes a non-None value.

    Args:
        setting: flat config field name; must be a ``ModelTuning`` field
            (AttributeError otherwise — a typo fails fast).
        model: the model id whose per-model default applies. An unregistered
            model simply has no per-model layer.
    """
    from shared.config import get_field

    # Membership check first: a non-tuning field must never resolve through this
    # path, even when it happens to carry an explicit value. Doing it here (not
    # only inside explain_setting) keeps the AttributeError ahead of the
    # get_field lookup, which would KeyError on a name that is no config field.
    getattr(DEFAULT_TUNING, setting)
    try:
        explicit = get_field(setting)
    except AttributeError:
        # The field's owning domain is not constructed in this process's
        # profile (Task #944): the tuning fields live in the AGENT domain, and
        # the gateway's token-usage / context-breakdown display endpoints call
        # resolve_context_budget too. Fail-fast is right for a typo, but this
        # is a legal cross-profile read — degrade to the registry floor and
        # let the agent process itself keep reading the explicit value.
        explicit = None
    return explain_setting(setting, model=model, explicit=explicit).value
