"""LLM config — LmSettings.

Split out of the former flat Settings god object; each field keeps its exact
env alias so the .env surface is unchanged. Aggregated by shared/config.
"""

from __future__ import annotations

from pydantic import Field, SecretStr

from shared.config._base import EnvSettings


class LmSettings(EnvSettings):
    llm_model: str = Field(
        default="deepseek-v4-pro",
        alias="AVA_MODEL",
        description=(
            "Agent model name, e.g. deepseek-v4-pro or claude-*. Per-agent "
            "overridable; a cross-provider override needs that provider's API key "
            "set on the host, or model build fails fast."
        ),
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": False,
            # capability=common: the cluster-wide spawn default, read by the
            # gateway (spawn pre-select / default-model endpoint) AND frozen
            # into agents at spawn — owned by neither capability alone.
            "capability": "common",
            # per_agent: gates the spawn/restart config-overlay (read by
            # shared/plugin_config_registry.py for both framework Settings and
            # plugin config fields). Orthogonal to scope: scope is the ownership
            # axis (drives bootstrap distribution); per_agent is the override
            # gate. They coexist on llm_model.
            "per_agent": True,
            "lifecycle": "frozen",
            "scope": "cluster-default",
        },
    )

    labeler_model: str = Field(
        # pro, not flash, by measurement (issue #178): prompts on this path are
        # frequently the long English second-person imperative brief that
        # `ava.agents.spawn()` writes, and flash *executes* that shape instead
        # of summarizing it — replaying real stored prompts, 15 of 56 attempts
        # answered the brief or emitted scaffolding, against 0 of 56 for pro.
        # Staying inside DeepSeek keeps the gateway's required-key surface
        # unchanged; the cheaper models that also scored 0 lost on output
        # quality (gpt-5.6-luna emitted stray private-use and Malayalam
        # codepoints into labels).
        default="deepseek-v4-pro",
        alias="AVA_LABELER_MODEL",
        description=(
            "Model used only to generate a conversation's short display name "
            "from its first message. Same provider matrix as llm_model; one "
            "cluster-wide value (no per-agent override). Prompts on this path "
            "are frequently machine-authored agent briefs, which a weaker model "
            "executes instead of summarizing — measure before downgrading."
        ),
        json_schema_extra={
            "restart_required": "",
            "writable": True,
            "sensitive": False,
            # The labeler daemon runs on the gateway side (services/labeler) —
            # its model key must survive the gateway profile pop.
            "capability": "gateway",
            "scope": "cluster-pinned",
        },
    )

    llm_non_streaming_fallback_timeout_seconds: float = Field(
        default=600.0,
        alias="AVA_LLM_NON_STREAMING_FALLBACK_TIMEOUT_SECONDS",
        description=(
            "Total timeout (seconds) for the non-streaming fallback after a "
            "corrupted stream. 600s leaves headroom for a slow thinking-model "
            "single response."
        ),
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    llm_invoke_timeout_seconds: float = Field(
        default=60.0,
        alias="AVA_LLM_INVOKE_TIMEOUT_SECONDS",
        description=(
            "Total timeout (seconds) for one non-streaming model invoke on the "
            "SDK paths (ava.understand, ava.web.fetch's answer step). Bounds a "
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
        alias="AVA_LLM_INVOKE_RETRY_ATTEMPTS",
        description=(
            "Retries after a timed-out or transiently-failed non-streaming "
            "invoke (ava.understand, ava.web.fetch answer). The call is "
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
        alias="AVA_LLM_INVOKE_RETRY_DELAY_SECONDS",
        description=(
            "Base wait (seconds) for non-streaming invoke retries; doubles "
            "per attempt (exponential backoff) up to "
            "AVA_LLM_INVOKE_RETRY_MAX_DELAY_SECONDS, plus up to 1s jitter."
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
        alias="AVA_LLM_INVOKE_RETRY_MAX_DELAY_SECONDS",
        description=(
            "Cap (seconds) on the exponential-backoff interval between "
            "non-streaming invoke retries (ava.understand / ava.web.fetch "
            "answer). The base AVA_LLM_INVOKE_RETRY_DELAY_SECONDS doubles per "
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

    llm_max_concurrent: str = Field(
        default="",
        alias="AVA_LLM_MAX_CONCURRENT",
        description=(
            "Per-provider outbound LLM concurrency caps, format "
            "'provider:limit,provider:limit' (e.g. 'deepseek:400,anthropic:200'; "
            "provider keys are the model prefixes: deepseek/claude/gpt/gemini/"
            "mimo/kimi/glm/qwen). Empty = unlimited (default). The cap wraps "
            "the whole SDK call (SDK-internal retries included) and queues "
            "excess calls instead of 429ing the provider. Reserved for when "
            "agent count or batch jobs approach a provider's account "
            "concurrency ceiling (DeepSeek: flash 2500 / pro 500); an unknown "
            "provider key fails fast at first use."
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
        alias="AVA_LLM_COMPACT_TIMEOUT_SECONDS",
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
        alias="AVA_LLM_STREAM_TTFT_TIMEOUT_SECONDS",
        description=(
            "Streaming time-to-first-token timeout (seconds): abort the turn if no "
            "first chunk arrives in time. Separate from the inter-chunk timeout so a "
            "slow cold-starting model isn't killed mid-stream. Unset resolves the "
            "per-model default (shared/lm/registry.py; shared floor 30). Raise for "
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

    llm_stream_inter_chunk_timeout_seconds: float | None = Field(
        default=None,
        alias="AVA_LLM_STREAM_INTER_CHUNK_TIMEOUT_SECONDS",
        description=(
            "Streaming inter-chunk timeout (seconds): abort the turn if the gap "
            "between chunks exceeds this. Unset resolves the per-model default "
            "(shared/lm/registry.py; shared floor 10). The floor is tight — raise "
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
        alias="AVA_LLM_RETRY_MAX_ATTEMPTS",
        description=(
            "Max LLM attempts per turn (initial + retries). Unset resolves the "
            "per-model default (shared/lm/registry.py; shared floor 6, sized for "
            "DeepSeek's intermittent tool-call drift)."
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
        alias="AVA_LLM_RETRY_INITIAL_INTERVAL_SECONDS",
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
        alias="AVA_LLM_RETRY_MAX_INTERVAL_SECONDS",
        description="Cap on the LLM retry backoff interval (seconds).",
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    llm_retry_max_consecutive_same_error: int = Field(
        default=3,
        alias="AVA_LLM_RETRY_MAX_CONSECUTIVE_SAME_ERROR",
        description=(
            "Fail fast after this many consecutive retries with the same exception "
            "type — a deterministic error won't be fixed by retrying. 0 disables "
            "(always exhaust retries)."
        ),
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    llm_fatal_provider_error_types: str = Field(
        default="engine_overloaded_error",
        alias="AVA_LLM_FATAL_PROVIDER_ERROR_TYPES",
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

    llm_silent_idle_max_consecutive: int = Field(
        default=3,
        alias="AVA_LLM_SILENT_IDLE_MAX_CONSECUTIVE",
        description=(
            "Max consecutive silent-idle turns (reasoning but no text and no "
            "tool_call) before halting to idle. Caps a model that habitually reasons "
            "without acting. 0 disables the cap (loop indefinitely)."
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
        alias="AVA_LLM_OVERRIDE",
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
        alias="AVA_REASONING_EFFORT",
        description=(
            "Cross-provider reasoning effort (`` / `none` / `minimal` / `low` / "
            "`medium` / `high` / `xhigh` / `max`). Unset resolves the per-model "
            "default (shared/lm/registry.py); an explicitly empty value pins each "
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
        alias="AVA_CLAUDE_THINKING_BUDGET_TOKENS",
        description=(
            "Extended-thinking token budget for Claude models that default thinking "
            "OFF (currently claude-haiku-4-5-20251001). 0 = explicitly leave off; when > 0 "
            "the API requires 1024 <= N < max_tokens; unset resolves the per-model "
            "default (shared/lm/registry.py; shared floor 0). Ignored on "
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
        default="deepseek-v4-flash",
        alias="AVA_UNDERSTAND_TEXT_MODEL",
        description="Model for `ava.understand`'s text path (strings and text files). Any agent model id works.",
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    understand_media_model: str = Field(
        default="gemini-3.5-flash",
        alias="AVA_UNDERSTAND_MEDIA_MODEL",
        description=(
            "Model for `ava.understand`'s media path (image/video/audio/PDF). "
            "The provider is picked by the model prefix through `build_chat_model` — "
            "any registered model accepting the media types works (default Gemini "
            "3.5 Flash, which natively decodes image/video/audio/PDF)."
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
        alias="AVA_UNDERSTAND_MEDIA_RESOLUTION",
        description=(
            "Media resolution for `ava.understand`'s media path: `low` / `medium` / "
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
        alias="AVA_UNDERSTAND_MEDIA_THINKING_LEVEL",
        description=(
            "Thinking level for `ava.understand`'s media path: `minimal` / `low` / "
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
        alias="AVA_UNDERSTAND_MEDIA_BASE_URL",
        description=(
            "Gemini endpoint override for `ava.understand`'s media path (the "
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

    gemini_explicit_cache_enabled: bool = Field(
        default=True,
        alias="AVA_GEMINI_EXPLICIT_CACHE_ENABLED",
        description=(
            "Pin the system prompt + execute_code tool schema into a Gemini explicit "
            "context cache (cachedContents API) instead of relying on implicit caching "
            "alone — cache-bound requests reference the cache and omit "
            "system_instruction/tools. Ignored by other providers; fail-open to "
            "implicit caching on any cache-layer error."
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

    gemini_cache_timeout_seconds: float = Field(
        default=15.0,
        alias="AVA_GEMINI_CACHE_TIMEOUT_SECONDS",
        description=(
            "Per-call timeout (seconds) for the Gemini explicit-cache API calls "
            "(caches.list / create / update). The google-genai SDK has no default "
            "timeout, so a wedged Gemini API would otherwise hold every agent's "
            "LLM-call prelude at the SDK default; the cache layer is fail-open, "
            "so a timeout falls back to implicit caching like any cache-layer error."
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

    deepseek_api_key: SecretStr | None = Field(
        default=None,
        alias="DEEPSEEK_API_KEY",
        description="DeepSeek API key. Empty = deepseek-* models unavailable.",
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": True,
            "scope": "cluster-pinned",
            "seed": True,
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
            "seed": True,
        },
    )

    gemini_api_key: SecretStr | None = Field(
        default=None,
        alias="GEMINI_API_KEY",
        description="Google Gemini API key, used by `ava.understand`'s media path and any gemini-* agent model. Empty = media understand raises.",
        json_schema_extra={
            # capability=common: consumed by memory_indexer (gateway-side
            # embeddings) AND agent-side Gemini calls.
            "capability": "common",
            "restart_required": "agent",
            "writable": True,
            "sensitive": True,
            "scope": "cluster-pinned",
            "seed": True,
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
            "seed": True,
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
            "seed": True,
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
            "seed": True,
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
            "seed": True,
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
            "seed": True,
        },
    )

    dashscope_base_url: str = Field(
        default="https://dashscope.aliyuncs.com/compatible-mode/v1",
        alias="AVA_DASHSCOPE_BASE_URL",
        description=(
            "OpenAI-compatible endpoint qwen* models dial. Defaults to Alibaba's "
            "public Beijing endpoint; a dedicated Model Studio workspace serves the "
            "same API on its own host "
            "(https://<workspace-id>.cn-beijing.maas.aliyuncs.com/compatible-mode/v1), "
            "which the default cannot reach. Keep the `/compatible-mode/v1` suffix — "
            "`/api/v1` on those hosts is the native protocol, not this one. Regions "
            "price differently, so pointing this at another region also means "
            "re-checking shared/lm/pricing_catalog.json."
        ),
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": False,
            # Seeded despite not being a secret: it is the other half of
            # DASHSCOPE_API_KEY, which IS seeded. A workspace key is minted for
            # one host and means nothing to the public default, so copying the
            # key alone into a fresh worktree cluster hands it a key it cannot
            # spend — the same lockout this field exists to fix.
            "seed": True,
            "scope": "cluster-pinned",
        },
    )
