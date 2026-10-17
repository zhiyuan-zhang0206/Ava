# meiva:note
# `LmSettings` (`base/config/domains/lm.py`) preserves the flat `.env` aliases for language-model settings. `llm_model` defaults to `deepseek-flash` with `capability="common"`, `scope="cluster-default"`, and `per_agent=True` so it serves as both the gateway spawn default and a per-agent overridable setting; `labeler_model` and `hierarchy_model` default to `deepseek-flash` with `capability="gateway"` so their provider keys survive the gateway profile pop; `understand_media_model` defaults to `gemini-3.8-flash`; and `gemini_api_key` carries `capability="common"` for both gateway-side embeddings and agent-side Gemini calls.
# meiva:link base/config/docs/process-profiles.meiva.okf.md
from __future__ import annotations

from pydantic import Field, SecretStr

from base.config.base import EnvSettings


class LmSettings(EnvSettings):
    llm_model: str = Field(
        default="deepseek-flash",
        alias="MEIVA_MODEL",
        description=(
            "Agent model name, e.g. deepseek-flash or claude-*. Per-agent "
            "overridable; a cross-provider override needs that provider's API key "
            "set on the host, or model build fails fast."
        ),
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": False,
            "capability": "common",
            "per_agent": True,
            "lifecycle": "frozen",
            "scope": "cluster-default",
        },
    )

    labeler_model: str = Field(
        default="deepseek-flash",
        alias="MEIVA_LABELER_MODEL",
        description=(
            "Model used only to generate a conversation's short display name "
            "from its first message. Same provider matrix as llm_model; one "
            "cluster-wide value (no per-agent override). Prompts on this path "
            "are frequently machine-authored agent briefs, which a weaker model "
            "executes instead of summarizing — measure before downgrading."
        ),
        json_schema_extra={
            "restart_required": "gateway",
            "writable": True,
            "sensitive": False,
            "capability": "gateway",
            "scope": "cluster-pinned",
        },
    )

    hierarchy_model: str = Field(
        default="deepseek-flash",
        alias="MEIVA_HIERARCHY_MODEL",
        description=(
            "Fallback model for the understanding-layer generation pass "
            "(hierarchical run-timeline node summaries, task #3704). The "
            "worker and the manual build normally generate on the TARGET "
            "agent's own effective model (task #4674) so requests hit the "
            "same provider-side prefix cache as the agent's own calls; this "
            "value is the last-resort fallback when that lookup fails. Cheap "
            "tier by design: a full day's recap cards measure at 0.7-0.9% of "
            "that day's agent input tokens at this tier. Staying inside "
            "DeepSeek keeps the gateway's required-key surface unchanged "
            "(same rationale as labeler_model)."
        ),
        json_schema_extra={
            "restart_required": "all",
            "writable": True,
            "sensitive": False,
            "capability": "gateway",
            "scope": "cluster-pinned",
        },
    )

    reply_suggestion_enabled: bool = Field(
        default=True,
        alias="MEIVA_REPLY_SUGGESTION_ENABLED",
        description=(
            "Cluster switch for reply suggestions: after an agent's turn ends the web "
            "console asks the gateway to draft a reply for the user, shown as grey text "
            "in the empty composer. Each user also has a `behavior.replySuggestions` "
            "preference. Off makes the endpoint answer with an empty suggestion without "
            "calling any model."
        ),
        json_schema_extra={
            "restart_required": "gateway",
            "writable": True,
            "sensitive": False,
            "capability": "gateway",
            "scope": "cluster-pinned",
        },
    )

    reply_suggestion_model: str = Field(
        default="deepseek-flash",
        alias="MEIVA_REPLY_SUGGESTION_MODEL",
        description=(
            "Chat model that drafts the suggested reply, run with reasoning off (a "
            "reasoning pass makes a one-line draft take tens of seconds). Must be a "
            "registered chat model; an unknown id fails the gateway at boot."
        ),
        json_schema_extra={
            "restart_required": "gateway",
            "writable": True,
            "sensitive": False,
            "capability": "gateway",
            "scope": "cluster-pinned",
        },
    )

    reply_suggestion_gate_model: str = Field(
        default="jev-1.13.0",
        alias="MEIVA_REPLY_SUGGESTION_GATE_MODEL",
        description=(
            "Decision model that judges whether a reply suggestion would help the user "
            "before any draft is generated. Must be a registered decision "
            "model (not a chat model); an unknown id fails the gateway at boot."
        ),
        json_schema_extra={
            "restart_required": "gateway",
            "writable": True,
            "sensitive": False,
            "capability": "gateway",
            "scope": "cluster-pinned",
        },
    )

    reply_suggestion_gate_threshold: float = Field(
        default=0.5,
        ge=0.0,
        le=1.0,
        alias="MEIVA_REPLY_SUGGESTION_GATE_THRESHOLD",
        description=(
            "Minimum probability (0-1) the gate model must give to 'a reply suggestion "
            "would help the user' for replies to be drafted. Raise it to draft less often."
        ),
        json_schema_extra={
            "restart_required": "gateway",
            "writable": True,
            "sensitive": False,
            "capability": "gateway",
            "scope": "cluster-pinned",
        },
    )

    reply_suggestion_max_candidates: int = Field(
        default=3,
        gt=0,
        alias="MEIVA_REPLY_SUGGESTION_MAX_CANDIDATES",
        description=(
            "Upper bound on reply candidates for one suggestion. Users reported that four "
            "or more candidates are noise: most turns deserve one reply, a real choice two, "
            "and three is the ceiling. The draft instruction states the bound; a longer "
            "model response is rejected as invalid_generation rather than truncated."
        ),
        json_schema_extra={
            "restart_required": "gateway",
            "writable": True,
            "sensitive": False,
            "capability": "gateway",
            "scope": "cluster-pinned",
        },
    )

    reply_suggestion_max_input_tokens: int = Field(
        default=1500,
        gt=0,
        alias="MEIVA_REPLY_SUGGESTION_MAX_INPUT_TOKENS",
        description=(
            "Token budget of the conversation excerpt (the user's latest message and the "
            "agent's last-turn text) given to the gate and the draft model. The user's "
            "message has the budget's first claim — a long agent turn cannot evict the "
            "message the suggestion answers — and whichever part must be cut keeps its "
            "newest words."
        ),
        json_schema_extra={
            "restart_required": "gateway",
            "writable": True,
            "sensitive": False,
            "capability": "gateway",
            "scope": "cluster-pinned",
        },
    )

    llm_invoke_timeout_seconds: float = Field(
        default=60.0,
        alias="MEIVA_LLM_INVOKE_TIMEOUT_SECONDS",
        description=(
            "Total timeout (seconds) for one non-streaming model invoke on the "
            "SDK paths (meiva.understand, meiva.web.fetch's answer step). Bounds a "
            "wedged provider that would otherwise hold the call at the provider "
            "SDK default (~600s). Applies per attempt; llm_invoke_retry_attempts "
            "restarts the whole bounded call."
        ),
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    llm_invoke_retry_attempts: int = Field(
        default=1,
        alias="MEIVA_LLM_INVOKE_RETRY_ATTEMPTS",
        description=(
            "Retries after a timed-out or transiently-failed non-streaming "
            "invoke (meiva.understand, meiva.web.fetch answer). The call is "
            "read-only/idempotent, so a bounded retry is safe; 0 disables."
        ),
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    llm_invoke_retry_delay_seconds: float = Field(
        default=2.0,
        alias="MEIVA_LLM_INVOKE_RETRY_DELAY_SECONDS",
        description=(
            "Base wait (seconds) for non-streaming invoke retries; doubles "
            "per attempt (exponential backoff) up to "
            "MEIVA_LLM_INVOKE_RETRY_MAX_DELAY_SECONDS, plus up to 1s jitter."
        ),
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    llm_invoke_retry_max_delay_seconds: float = Field(
        default=30.0,
        alias="MEIVA_LLM_INVOKE_RETRY_MAX_DELAY_SECONDS",
        description=(
            "Cap (seconds) on the exponential-backoff interval between "
            "non-streaming invoke retries (meiva.understand / meiva.web.fetch "
            "answer). The base MEIVA_LLM_INVOKE_RETRY_DELAY_SECONDS doubles per "
            "attempt up to this cap, plus up to 1s jitter; a provider "
            "Retry-After header asking for longer than the backoff is "
            "respected (capped at 120s)."
        ),
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    llm_compact_timeout_seconds: float = Field(
        default=120.0,
        alias="MEIVA_LLM_COMPACT_TIMEOUT_SECONDS",
        description=(
            "Total timeout (seconds) for one compaction-summary generation "
            "(auto-compact + compact_request). The summary call is larger than "
            "a plain invoke, hence a wider default than "
            "llm_invoke_timeout_seconds; bounds an agent wedged mid-compact."
        ),
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    llm_stream_ttft_timeout_seconds: float | None = Field(
        default=None,
        alias="MEIVA_LLM_STREAM_TTFT_TIMEOUT_SECONDS",
        description=(
            "Streaming time-to-first-token timeout (seconds): abort the turn if no "
            "first chunk arrives in time. Separate from the inter-chunk timeout so a "
            "slow cold-starting model isn't killed mid-stream. Unset resolves the "
            "per-model default (base/lm/registry.py; shared floor 30). Raise for "
            "slow providers; per-agent overridable."
        ),
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-default",
            "per_agent": True,
            "lifecycle": "live",
        },
    )

    llm_stream_total_timeout_seconds: float | None = Field(
        default=None,
        alias="MEIVA_LLM_STREAM_TOTAL_TIMEOUT_SECONDS",
        description=(
            "Total duration ceiling (seconds) for one streaming LLM attempt, "
            "even while chunks keep arriving. This bounds a drip-fed stream and "
            "is independent of the TTFT and inter-chunk gap timeouts. Unset "
            "resolves the per-model default (base/lm/registry.py; shared floor "
            "3600). Per-agent overridable."
        ),
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-default",
            "per_agent": True,
            "lifecycle": "live",
        },
    )

    llm_stream_inter_chunk_timeout_seconds: float | None = Field(
        default=None,
        alias="MEIVA_LLM_STREAM_INTER_CHUNK_TIMEOUT_SECONDS",
        description=(
            "Streaming inter-chunk timeout (seconds): abort the turn if the gap "
            "between chunks exceeds this. Unset resolves the per-model default "
            "(base/lm/registry.py; shared floor 10). The floor is tight — raise "
            "it on false positives."
        ),
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-default",
            "per_agent": True,
            "lifecycle": "live",
        },
    )

    llm_retry_max_attempts: int | None = Field(
        default=None,
        alias="MEIVA_LLM_RETRY_MAX_ATTEMPTS",
        description=(
            "Max LLM attempts per turn (initial + retries). Unset resolves the "
            "per-model default (base/lm/registry.py; shared floor 6, sized for "
            "DeepSeek's intermittent tool-call drift)."
        ),
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    llm_retry_max_total_seconds: float = Field(
        default=420.0,
        gt=0,
        alias="MEIVA_LLM_RETRY_MAX_TOTAL_SECONDS",
        description=(
            "Hard wall-clock budget (seconds) across an LLM node's initial "
            "attempt, retries, and retry waits. The retry policy clips its next "
            "wait to the remaining budget and halts instead of invoking again "
            "after it expires."
        ),
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    llm_retry_initial_interval_seconds: float = Field(
        default=30.0,
        alias="MEIVA_LLM_RETRY_INITIAL_INTERVAL_SECONDS",
        description="First LLM retry wait (seconds); doubles each attempt (30→60→120→…).",
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    llm_retry_max_interval_seconds: float = Field(
        default=480.0,
        alias="MEIVA_LLM_RETRY_MAX_INTERVAL_SECONDS",
        description="Cap on the LLM retry backoff interval (seconds).",
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    llm_fatal_provider_error_types: str = Field(
        default="engine_overloaded_error",
        alias="MEIVA_LLM_FATAL_PROVIDER_ERROR_TYPES",
        description=(
            "Comma-separated provider error `type` strings (from the response body "
            "`error.type`) treated as fatal — fail fast, no retry. Empty disables "
            "error-type fast-fail."
        ),
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-default",
            "per_agent": True,
            "lifecycle": "live",
        },
    )

    llm_silent_idle_max_output_tokens: int = Field(
        default=2048,
        alias="MEIVA_LLM_SILENT_IDLE_MAX_OUTPUT_TOKENS",
        description=(
            "Cumulative output-token budget ceiling for consecutive silent-idle "
            "turns (reasoning but no text and no tool_call). A provider-reported "
            "zero-output silent idle still consumes one budget token, so it cannot "
            "loop indefinitely. The next silent idle that reaches the ceiling halts "
            "instead of spending another model call. 0 disables the cost boundary."
        ),
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    llm_override: str = Field(
        default="",
        alias="MEIVA_LLM_OVERRIDE",
        description="Test-only fake-chat-model injection, format `module.path:factory_name`. Empty = use the real provider.",
        json_schema_extra={
            "restart_required": "agent",
            "writable": False,
            "sensitive": False,
            "scope": "agent",
        },
    )

    reasoning_effort: str | None = Field(
        default=None,
        alias="MEIVA_REASONING_EFFORT",
        description=(
            "Cross-provider reasoning effort (`` / `none` / `minimal` / `low` / "
            "`medium` / `high` / `xhigh` / `max`). Unset resolves the per-model "
            "default (base/lm/registry.py); an explicitly empty value pins each "
            "provider's own default. Mapped and clamped per provider at model "
            "build; unknown values fail fast. Per-agent overridable."
        ),
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-default",
            "per_agent": True,
            "lifecycle": "frozen",
        },
    )

    claude_thinking_budget_tokens: int | None = Field(
        default=None,
        alias="MEIVA_CLAUDE_THINKING_BUDGET_TOKENS",
        description=(
            "Extended-thinking token budget for Claude models that default thinking "
            "OFF (currently claude-haiku-4-5-20251001). 0 = explicitly leave off; when > 0 "
            "the API requires 1024 <= N < max_tokens; unset resolves the per-model "
            "default (base/lm/registry.py; shared floor 0). Ignored on "
            "adaptive-thinking models (sonnet / opus / fable) — tune those via "
            "reasoning_effort."
        ),
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-default",
            "per_agent": True,
            "lifecycle": "frozen",
        },
    )

    understand_text_model: str = Field(
        default="deepseek-flash",
        alias="MEIVA_UNDERSTAND_TEXT_MODEL",
        description="Model for `meiva.understand`'s text path (strings and text files). Any agent model id works.",
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    understand_media_model: str = Field(
        default="gemini-3.8-flash",
        alias="MEIVA_UNDERSTAND_MEDIA_MODEL",
        description=(
            "Model for `meiva.understand`'s media path (image/video/audio/PDF). "
            "Must be a Gemini model — the media wire format is Gemini-specific, "
            "so a non-Gemini id fails fast with a clear error. The provider "
            "abstraction (factory routing / endpoint override / quality knobs) "
            "is in place for a second media provider to plug in later."
        ),
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    understand_media_resolution: str = Field(
        default="high",
        alias="MEIVA_UNDERSTAND_MEDIA_RESOLUTION",
        description=(
            "Media resolution for `meiva.understand`'s media path: `low` / `medium` / "
            "`high`. Higher costs more input tokens per image/frame; drop it to cut "
            "cost on bulk media."
        ),
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    understand_media_thinking_level: str = Field(
        default="medium",
        alias="MEIVA_UNDERSTAND_MEDIA_THINKING_LEVEL",
        description=(
            "Thinking level for `meiva.understand`'s media path: `minimal` / `low` / "
            "`medium` / `high`. Higher = deeper but slower and pricier."
        ),
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    understand_media_base_url: str | None = Field(
        default=None,
        alias="MEIVA_UNDERSTAND_MEDIA_BASE_URL",
        description=(
            "Gemini endpoint override for `meiva.understand`'s media path (the "
            "`generativelanguage.googleapis.com` base). None = the SDK default "
            "official endpoint. Point it at a self-hosted relay or a provider "
            "mirror without code changes; the same auth as Gemini is sent "
            "(GEMINI_API_KEY). Non-gemini media models ignore it."
        ),
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    deepseek_api_key: SecretStr | None = Field(
        default=None,
        alias="DEEPSEEK_API_KEY",
        description="DeepSeek API key. Empty = deepseek-* models unavailable.",
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": True,
            "scope": "cluster-pinned",
        },
    )

    anthropic_api_key: SecretStr | None = Field(
        default=None,
        alias="ANTHROPIC_API_KEY",
        description="Anthropic API key. Empty = claude-* models unavailable.",
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": True,
            "scope": "cluster-pinned",
        },
    )

    gemini_api_key: SecretStr | None = Field(
        default=None,
        alias="GEMINI_API_KEY",
        description="Google Gemini API key, used by `meiva.understand`'s media path and any gemini-* agent model. Empty = media understand raises.",
        json_schema_extra={
            "capability": "common",
            "restart_required": "agent",
            "writable": True,
            "sensitive": True,
            "scope": "cluster-pinned",
        },
    )

    openai_api_key: SecretStr | None = Field(
        default=None,
        alias="OPENAI_API_KEY",
        description="OpenAI API key. Empty = gpt-* models unavailable.",
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": True,
            "scope": "cluster-pinned",
        },
    )

    xiaomi_api_key: SecretStr | None = Field(
        default=None,
        alias="MIMO_API_KEY",
        description="Xiaomi MiMo API key. Empty = mimo-* models unavailable.",
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": True,
            "scope": "cluster-pinned",
        },
    )

    moonshot_api_key: SecretStr | None = Field(
        default=None,
        alias="MOONSHOT_API_KEY",
        description="Moonshot Kimi API key. Empty = kimi-* models unavailable.",
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": True,
            "scope": "cluster-pinned",
        },
    )

    zhipu_api_key: SecretStr | None = Field(
        default=None,
        alias="GLM_API_KEY",
        description="Zhipu GLM API key. Empty = glm-* models unavailable.",
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": True,
            "scope": "cluster-pinned",
        },
    )

    dashscope_api_key: SecretStr | None = Field(
        default=None,
        alias="DASHSCOPE_API_KEY",
        description=(
            "Alibaba Cloud Model Studio (DashScope) API key, the platform serving "
            "Qwen. Empty = qwen* models unavailable."
        ),
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": True,
            "scope": "cluster-pinned",
        },
    )

    dashscope_base_url: str = Field(
        default="https://dashscope.aliyuncs.com/compatible-mode/v1",
        alias="MEIVA_DASHSCOPE_BASE_URL",
        description=(
            "OpenAI-compatible endpoint qwen* models dial. Defaults to Alibaba's "
            "public Beijing endpoint; a dedicated Model Studio workspace serves the "
            "same API on its own host "
            "(https://<workspace-id>.cn-beijing.maas.aliyuncs.com/compatible-mode/v1), "
            "which the default cannot reach. Keep the `/compatible-mode/v1` suffix — "
            "`/api/v1` on those hosts is the native protocol, not this one. Regions "
            "price differently, so pointing this at another region also means "
            "re-checking base/lm/pricing/pricing_catalog_archive.json."
        ),
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )
