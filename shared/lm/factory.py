"""LLM provider factory — picks LangChain's chat model by model name prefix.

For test / multi-instance / debug scenarios, a custom factory can be
injected via `AVA_LLM_OVERRIDE=mod:factory` env (see tests/e2e/README.md);
if the env is not set, the original path is used.


Current provider matrix:

| prefix       | provider  | LangChain pkg              | API key env         | base_url                          |
|--------------|-----------|----------------------------|---------------------|-----------------------------------|
| `claude-*`   | Anthropic | `langchain-anthropic`      | `ANTHROPIC_API_KEY` | (default)                         |
| `deepseek-*` | DeepSeek  | `langchain-anthropic`      | `DEEPSEEK_API_KEY`  | `https://api.deepseek.com/anthropic` |
| `gemini-*`   | Google    | `langchain-google-genai`   | `GEMINI_API_KEY`    | (default)                         |
| `gpt-*`      | OpenAI    | `langchain-openai`         | `OPENAI_API_KEY`    | (default)                         |
| `mimo-*`     | Xiaomi    | `ReasoningContentChatModel`| `MIMO_API_KEY`      | `https://api.xiaomimimo.com/v1`   |
| `kimi-*`     | Moonshot  | `langchain-moonshot`        | `MOONSHOT_API_KEY`  | (default)                         |
| `glm-*`      | Zhipu     | `ReasoningContentChatModel`| `GLM_API_KEY`       | `https://open.bigmodel.cn/api/paas/v4` |
| `grok-*`     | xAI       | `langchain-xai`             | `XAI_API_KEY`       | (default)                         |

kimi uses `ChatMoonshot` (`langchain-moonshot`); grok uses `ChatXAI`
(`langchain-xai`). Both capture reasoning in `additional_kwargs["reasoning_content"]`
(not canonical content blocks) — the streaming fan-out (`RedisStreamHandler`)
and timeline (`shared/timeline.py`) handle both styles.

glm / mimo use `ReasoningContentChatModel` (`shared/lm/_reasoning_compat.py`), a
ChatOpenAI subclass folding `reasoning_content` deltas into canonical
`{"type":"thinking", ...}` blocks — neither has a suitable community package
(`langchain-zhipuai` unmaintained; `langchain_zhipu` needs `langchain<0.3.0`;
MiMo has none at all).

**deepseek-* goes through ChatAnthropic, not langchain-deepseek**:
langchain-deepseek 1.0.1 on the thinking + tool calls + streaming path
intermittently produces broken AIMessage (all metadata empty), and the next
turn 400s with "reasoning_content must be passed back" killing the process.
13 production threads hit it; upstream issue #34166 is still OPEN, three fix
PRs (#35067/#35620/#37065) closed without merging. DeepSeek's
anthropic-compatible endpoint (`https://api.deepseek.com/anthropic`) speaks
the Anthropic Messages protocol instead (thinking in `content[type=thinking]`
blocks with signature, echoed transparently) — ChatAnthropic handles it out
of the box, bypassing the broken reasoning_content roundtrip entirely.

Adding a provider just adds one `_build_*_model` helper in `shared/lm/_providers.py` + one dispatch line in `build_chat_model`.

**`max_tokens` + reasoning effort dispatching** — per-model facts (output caps,
effort vocabularies) live in `shared/lm/registry.py` (`MODELS`); the
per-provider clamp machinery lives in the companion module `shared/lm/_effort.py`
(its docstring has the detail). In short: the two anthropic-protocol branches
pin max_tokens explicitly to the model's documented output cap
(`ModelSpec.max_output_tokens`) and fail fast on unregistered models —
langchain-anthropic's bundled profile table falls back to a legacy 4096 default
for unknown ids (#169 truncation incident). max_tokens is the server-side
output cap, not a budget — setting to cap does not increase generation. The
OpenAI-style branches leave it unset (those APIs default to the model's own
cap). The reasoning effort (`resolve_setting("reasoning_effort", ...)`:
explicit env/overlay value, else the model's registry default, else the
provider default) maps per branch onto what each provider accepts via
`_clamp_effort` — out-of-range values clamp (logged), unknown strings fail
fast at build time instead of as a provider 400 mid-run.

Streaming / usage_metadata: providers attach `usage_metadata` on the final
chunk; `AIMessageChunk += chunk` accumulation in `agent/graph/_llm.py::llm_node`
is followed by `message_chunk_to_message` to preserve usage; an assert before
entering state guards that metadata is not empty.
"""

from __future__ import annotations

import importlib
from typing import Any, Protocol

from langchain_core.language_models.chat_models import BaseChatModel
from loguru import logger

from shared.config import field_alias, get_field, settings

# Reasoning-effort dispatch lives in the companion module shared/lm/_effort.py
# (split for the file-size ceiling); per-model facts live in
# shared/lm/registry.py. Both are re-imported here so factory stays the catalog
# import surface for callers and tests.
from shared.lm._effort import (
    _clamp_effort as _clamp_effort,
)  # re-exported (tests import it via factory)
from shared.lm._providers import (
    _GROK_CONV_ID_NAMESPACE as _GROK_CONV_ID_NAMESPACE,  # re-exported (tests use it)
)
from shared.lm._providers import (
    _PROCESS_CONV_ID as _PROCESS_CONV_ID,  # re-exported (tests use it)
)
from shared.lm._providers import (
    ThinkingConfig,
    _build_claude_model,
    _build_deepseek_model,
    _build_gemini_model,
    _build_glm_model,
    _build_gpt_model,
    _build_grok_model,
    _build_kimi_model,
    _build_mimo_model,
)
from shared.lm.registry import (
    MODEL_CONTEXT_WINDOW as MODEL_CONTEXT_WINDOW,  # re-exported catalog view
)
from shared.lm.registry import (
    MODEL_IDENTITY as MODEL_IDENTITY,  # re-exported catalog view
)
from shared.lm.registry import (
    MODEL_KNOWLEDGE_CUTOFF as MODEL_KNOWLEDGE_CUTOFF,  # re-exported catalog view
)
from shared.lm.registry import (
    MODELS,
    resolve_setting,
)
from shared.lm.registry import (
    SUPPORTED_MODELS as SUPPORTED_MODELS,  # re-exported catalog view
)


class _LLMFactory(Protocol):
    """Contract for the callable that `AVA_LLM_OVERRIDE=mod:factory` points to.

    Newly written fake factories should conform to this signature: take
    model name, return a BaseChatModel subclass. `_resolve_override` runs
    isinstance(BaseChatModel) validation at the end; bad factories blow up
    at build time rather than crashing deep in the graph.
    """

    def __call__(self, model: str) -> BaseChatModel: ...


# Model prefixes whose provider binding decodes native image content blocks.
# claude / gemini / gpt are multimodal on the endpoints Ava binds; kimi / grok
# accept image input on their OpenAI-compatible endpoints. deepseek (bound to
# its anthropic-compatible endpoint above), mimo, and glm are text-only there,
# so a HumanMessage carrying an image block would 400 (or be silently dropped)
# mid-turn. The message endpoint gates on this to 422 an image addressed to a
# text-only agent up front, rather than letting the LLM call fail after the
# inbound is already queued. Add a prefix here the day its provider ships vision
# on the bound endpoint.
_VISION_MODEL_PREFIXES: tuple[str, ...] = ("claude-", "gemini-", "gpt-", "kimi-", "grok-")


def model_supports_vision(model: str) -> bool:
    """Whether `model`'s provider binding accepts native image content blocks."""
    return model.startswith(_VISION_MODEL_PREFIXES)


def provider_key_of_model(model: str) -> str | None:
    """Provider key for a model name, or None for an unregistered prefix.

    Keys are the `_MODEL_KEY_MAP` prefixes with the trailing dash stripped
    (`deepseek` / `claude` / `gpt` / `gemini` / `mimo` / `kimi` / `glm` /
    `grok`) — the same keys `AVA_LLM_MAX_CONCURRENT` accepts
    (`shared/lm/_concurrency.py`). None means the limiter passes through.
    """
    for prefix in _MODEL_KEY_MAP:
        if model.startswith(prefix):
            return prefix[:-1]
    return None


# Model prefix → (provider display name, settings attribute, env var name).
# Single-source mapping used by both build_chat_model and validate_model_config.
# An entry here plus a build_chat_model branch is the dispatch half only — a new
# vendor also needs a registry.py MODELS entry, a _providers.py builder, a
# config/lm.py key field, usually an _effort.py vocabulary, and a stop.py entry
# when its client emits a model_provider string stop.py does not already carry.
# The full per-vendor cost, and the plan to make it a plugin concern instead, are
# in future/infra/model-providers-as-plugins.md (summarized in
# shared/lm/lm.ava.okf.md).
_MODEL_KEY_MAP: dict[str, tuple[str, str, str]] = {
    "claude-": ("Anthropic", "anthropic_api_key", "ANTHROPIC_API_KEY"),
    "deepseek-": ("DeepSeek", "deepseek_api_key", "DEEPSEEK_API_KEY"),
    "gemini-": ("Google", "gemini_api_key", "GEMINI_API_KEY"),
    "gpt-": ("OpenAI", "openai_api_key", "OPENAI_API_KEY"),
    "mimo-": ("Xiaomi", "xiaomi_api_key", "MIMO_API_KEY"),
    "kimi-": ("Moonshot", "moonshot_api_key", "MOONSHOT_API_KEY"),
    "glm-": ("Zhipu", "zhipu_api_key", "GLM_API_KEY"),
    "grok-": ("xAI", "xai_api_key", "XAI_API_KEY"),
}


def validate_model_config(
    *,
    model: str | None = None,
    config: dict[str, object] | None = None,
) -> str:
    """Validate that the given model config can be spawned.

    Called at the spawn boundary (gateway POST /api/agents handler) to fail
    fast before forwarding to the runner — a 400 with a clear message is
    better than an agent process starting and silently hanging.

    Resolves the effective model from ``config.llm_model`` first, falling back
    to ``model`` (the cluster default), then checks:
    1. The model name is registered in SUPPORTED_MODELS.
    2. The required API key for that model's provider is configured.

    Args:
        model: fallback model name (cluster default). Ignored when
            ``config["llm_model"]`` is set.
        config: per-agent config overlay, may contain ``llm_model``.

    Returns:
        The resolved model name on success — the caller may use it directly.

    Raises:
        ValueError: model unknown or its API key is not configured. The
            message is user-facing (fit for an HTTP 400 body).
    """
    # Resolve effective model: per-agent overlay wins over cluster default.
    effective_model: str | None = None
    if config is not None:
        m = config.get("llm_model")
        if isinstance(m, str):
            effective_model = m
    if effective_model is None and model is not None:
        effective_model = model
    if effective_model is None:
        raise ValueError(
            "no model configured — set llm_model in cluster config or pass "
            "config.llm_model in the spawn request"
        )

    # 1. Model must be registered.
    all_models: list[str] = [m for models in SUPPORTED_MODELS.values() for m in models]
    if effective_model not in all_models:
        raise ValueError(
            f"unknown model {effective_model!r}. Available models: " + ", ".join(sorted(all_models))
        )

    # 2. API key must be configured — unless an LLM override is active
    # (e2e tests inject fake chat models via AVA_LLM_OVERRIDE and don't need
    # real keys; the override path in build_chat_model skips the real LLM).
    if settings.lm.llm_override:
        return effective_model

    _ensure_provider_key(effective_model)
    return effective_model


def _ensure_provider_key(effective_model: str) -> None:
    """Fail fast when the effective model's provider API key is not configured.

    Drives the lookup from `_MODEL_KEY_MAP` — the same single-source map the
    provider branches dispatch on. An unregistered model that slipped past
    SUPPORTED_MODELS raises with a pointer to the map (provider map out of
    sync with SUPPORTED_MODELS).
    """
    for prefix, (_provider, attr, env_var) in _MODEL_KEY_MAP.items():
        if effective_model.startswith(prefix):
            key = get_field(attr)
            if key is None:
                # The gateway profile pops provider keys from os.environ
                # (per-process env assembly, Task #856) while the cluster's
                # .env file stays the authoritative configuration source.
                # Fall back to the file so the spawn boundary can still
                # fail fast on a genuinely missing key without carrying the
                # secret in the gateway process env (regression: #1562
                # popped the keys and every spawn 400'd).
                from shared.runtime_config import read_env_aliases

                if field_alias(attr) in read_env_aliases():
                    return
                raise ValueError(
                    f"{effective_model} requires {env_var} which is not "
                    "configured — set it in ~/.ava/.env or export before "
                    "spawning"
                )
            return
    raise ValueError(
        f"no provider mapping for model {effective_model!r} — "
        "add its prefix to shared/lm/factory.py:_MODEL_KEY_MAP"
    )


def _resolve_override(override: str, model: str) -> BaseChatModel:
    """Parse `AVA_LLM_OVERRIDE=mod:factory` env; report failure errors in four
    classes hierarchically ("format / module not found / factory not found /
    return type wrong"), so the user can pinpoint."""
    module_path, sep, factory_name = override.partition(":")
    if not sep or not module_path or not factory_name.isidentifier():
        raise ValueError(
            f"AVA_LLM_OVERRIDE={override!r}: requires 'module.path:factory_name' form "
            f"(factory_name must be a valid Python identifier)"
        )
    try:
        module = importlib.import_module(module_path)
    except ImportError as e:
        raise ImportError(
            f"AVA_LLM_OVERRIDE={override!r}: cannot find module {module_path!r} ({e})"
        ) from e
    factory: _LLMFactory | None = getattr(module, factory_name, None)
    if factory is None:
        raise AttributeError(
            f"AVA_LLM_OVERRIDE={override!r}: module {module_path!r} has no attribute {factory_name!r}"
        )
    result = factory(model)
    if not isinstance(result, BaseChatModel):
        raise TypeError(
            f"AVA_LLM_OVERRIDE={override!r}: factory returned {type(result).__name__!r}, "
            f"not a BaseChatModel subclass"
        )
    return result


def build_chat_model(
    model: str,
    *,
    thinking: ThinkingConfig | None = None,
    reasoning_effort: str | None = None,
    streaming: bool | None = None,
    timeout: float | None = None,
) -> BaseChatModel:
    """Pick the provider by model name prefix and return the corresponding ChatModel.

    The returned ChatModel does not have tools bound — the caller binds them
    at use site via `llm.bind_tools([execute_code])`. This way paths that
    don't need tools (compaction etc.) can use the same ChatModel instance.

    Dispatches to the per-provider `_build_*_model` helpers in
    `shared/lm/_providers.py` (each documents its own key / thinking /
    reasoning-effort wiring). The cross-provider resolution shared by every
    branch happens here: `AVA_LLM_OVERRIDE`, the streaming default, and the
    reasoning-effort knob (`resolved_effort` — explicit env/.env/overlay
    value wins, else the model's registry default, else the provider default).

    Args:
        model: e.g. `claude-sonnet-5` / `deepseek-v4-pro`.
        thinking: cross-provider thinking switch (Anthropic Messages API
            shape). `{"type": "disabled"}` turns reasoning off where the
            provider supports it (short-text paths like label generation —
            thinking is slow/expensive and turns content into list-of-blocks);
            it also skips reasoning-effort injection where both would conflict
            (deepseek 400) or contradict intent (claude / glm / gemini /
            mimo). kimi-k3 / grok-4.5 cannot disable reasoning — logged and
            ignored. `{"type": "enabled", "budget_tokens": N}` is manual
            extended thinking — see the claude helper for the per-model rules.
            None = provider default.
        reasoning_effort: deepseek-* only — when set, overrides the resolved
            per-model effort for this call, clamped onto the model's
            `effort_levels` (`high` / `max`); `none` turns reasoning off
            through the thinking switch (the endpoint's effort vocabulary has
            no off level). Ignored when thinking is disabled (server rejects
            both) and on non-deepseek models.
        streaming: whether to enable LLM streaming. None (default) resolves
            from the model's registry entry (`ModelSpec.streaming` — True for
            every spawnable model). Explicit True/False overrides the model
            default; this is a construction-time default, not a retry policy
            (`_consume_llm`'s fatal-provider-error fallback stays active
            regardless).

        timeout: per-request wall-clock ceiling passed to the provider
            client (request_timeout on anthropic-protocol providers,
            timeout elsewhere). None = the provider SDK's own default. The
            agent's streaming path deliberately leaves it None (its own
            TTFT / inter-chunk timeouts govern); the non-streaming SDK
            paths (ava.understand / ava.web.fetch answer) pass
            settings.lm.llm_invoke_timeout_seconds so a wedged provider
            surfaces in tens of seconds instead of the SDK default (~600s).

    Raises:
        ValueError: model prefix did not match — prompts to add a branch.
        RuntimeError: a provider path needs its API key env; if missing,
            blows up immediately rather than reaching a server 401.
    """
    # e2e tests inject fake chat model via AVA_LLM_OVERRIDE (tests/e2e/README.md);
    # if set, warn loudly — a dev accidentally leaving it in .env would route
    # all agents through a fake LLM, and production observability must be fail-loud.
    override = settings.lm.llm_override
    if override:
        logger.warning(
            f"AVA_LLM_OVERRIDE active: model={model!r} does not go through real LLM, routed via {override!r}"
        )
        return _resolve_override(override, model)

    # Start from the caller's thinking dict when given; provider branches may
    # still rewrite it (claude display=summarized opt-in / haiku budget opt-in).
    extra_kwargs: dict[str, Any] = {}
    if thinking is not None:
        extra_kwargs["thinking"] = thinking

    # Resolve streaming: explicit kwarg overrides model default, model default
    # overrides the fallback True (kimi defaults to True — streaming-first with
    # a non-streaming fallback on 429).
    spec = MODELS.get(model)
    if streaming is None:
        streaming = spec.streaming if spec is not None else True
    disable_streaming = not streaming
    if disable_streaming:
        extra_kwargs["disable_streaming"] = True

    # The cross-provider reasoning-effort knob, resolved per model: explicit
    # env/.env/overlay value wins, else the model's registry default, else "".
    resolved_effort: str = resolve_setting("reasoning_effort", model=model)

    if model.startswith("claude-"):
        return _build_claude_model(
            model, spec, thinking, resolved_effort, extra_kwargs, timeout=timeout
        )
    if model.startswith("deepseek-"):
        return _build_deepseek_model(
            model,
            spec,
            thinking,
            reasoning_effort,
            resolved_effort,
            extra_kwargs,
            timeout=timeout,
        )
    if model.startswith("gemini-"):
        return _build_gemini_model(
            model,
            thinking=thinking,
            resolved_effort=resolved_effort,
            disable_streaming=disable_streaming,
            timeout=timeout,
        )
    if model.startswith("gpt-"):
        return _build_gpt_model(
            model,
            thinking=thinking,
            resolved_effort=resolved_effort,
            disable_streaming=disable_streaming,
            timeout=timeout,
        )
    if model.startswith("mimo-"):
        return _build_mimo_model(
            model,
            thinking=thinking,
            resolved_effort=resolved_effort,
            disable_streaming=disable_streaming,
            timeout=timeout,
        )
    if model.startswith("kimi-"):
        return _build_kimi_model(
            model,
            thinking=thinking,
            resolved_effort=resolved_effort,
            disable_streaming=disable_streaming,
            timeout=timeout,
        )
    if model.startswith("glm-"):
        return _build_glm_model(
            model,
            thinking=thinking,
            resolved_effort=resolved_effort,
            disable_streaming=disable_streaming,
            timeout=timeout,
        )
    if model.startswith("grok-"):
        return _build_grok_model(
            model,
            thinking=thinking,
            resolved_effort=resolved_effort,
            disable_streaming=disable_streaming,
            timeout=timeout,
        )

    raise ValueError(
        f"Unknown model {model!r} — add a {model.split('-', maxsplit=1)[0]}-* "
        "prefix branch in `shared/lm/_providers.py`"
    )
