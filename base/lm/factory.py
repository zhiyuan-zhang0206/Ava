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
| `qwen*`      | Alibaba   | `ReasoningContentChatModel`| `DASHSCOPE_API_KEY` | `AVA_DASHSCOPE_BASE_URL` (default `https://dashscope.aliyuncs.com/compatible-mode/v1`) |

kimi uses `ChatMoonshot` (`langchain-moonshot`) and captures reasoning in
`additional_kwargs["reasoning_content"]` (not canonical content blocks) — the
streaming fan-out (`RedisStreamHandler`) and timeline (`base/agents/history/timeline.py`)
handle that style. Its binding lives in `ava_builtins/plugins/lm_moonshot`.

glm / mimo / qwen use `ReasoningContentChatModel` (`base/lm/compat/openai_reasoning.py`), a
ChatOpenAI subclass folding `reasoning_content` deltas into canonical
`{"type":"thinking", ...}` blocks — none has a suitable community package
(`langchain-zhipuai` unmaintained; `langchain_zhipu` needs `langchain<0.3.0`;
MiMo has none at all; Alibaba ships a `dashscope` SDK that is not a LangChain
client and its own docs drive the OpenAI-compatible endpoint with the OpenAI SDK).
Their bindings live in the corresponding provider plugins.

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

Adding a provider means adding a `provider.py` beside a plugin's `plugin.py`
(`base/lm/docs/model-providers-as-plugins.md`; contract in
`base/lm/provider_api.py`, loaded by `base/lm/plugin_providers.py`).

**`max_tokens` + reasoning effort dispatching** — per-model facts (output caps,
effort vocabularies) live in `base/lm/registry.py` (`ModelSpec`, held by the catalog); the
per-model effort validation lives in the companion module `base/lm/effort.py`
(its docstring has the detail). In short: the two anthropic-protocol branches
pin max_tokens explicitly to the model's documented output cap
(`ModelSpec.max_output_tokens`) and fail fast on unregistered models —
langchain-anthropic's bundled profile table falls back to a legacy 4096 default
for unknown ids (#169 truncation incident). max_tokens is the server-side
output cap, not a budget — setting to cap does not increase generation. The
OpenAI-style branches leave it unset (those APIs default to the model's own
cap). The reasoning effort (the `reasoning_effort` setting through `resolve_setting`:
the agent's or the cluster's explicit value, else the model's registry default, else the
provider default) is validated against the selected model's declared options.
`validate_effort` preserves supported values and rejects unsupported or unknown
strings at build time; it never substitutes another graded effort.

Streaming / usage_metadata: providers attach `usage_metadata` on the final
chunk; `AIMessageChunk += chunk` accumulation in `agent/graph/llm/node.py::llm_node`
is followed by `message_chunk_to_message` to preserve usage; an assert before
entering state guards that metadata is not empty.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    # Annotation-only at module scope (`_LLMFactory`, `build_chat_model`);
    # the runtime isinstance check in `_resolve_override` imports it at the
    # call site. Keeps the chat-model stack off the provider-registration path
    # (exec-child boot, task #3633; `_TYPE_CHECKING_ALLOWED`).
    from langchain_core.language_models.chat_models import BaseChatModel
from loguru import logger

from base.host.env.agent_slices import ModelOverrides
from base.lm import provider_api
from base.lm.catalog import ModelCatalog

# Reasoning-effort dispatch lives in the companion module base/lm/effort.py
# (split for the file-size ceiling); per-model facts and the media-capability
# resolution live in base/lm/registry.py. Both are re-imported here so
# factory stays the catalog import surface for callers and tests.
from base.lm.effort import (
    validate_effort as validate_effort,
)  # re-exported (tests import it via factory)
from base.lm.provider_api import ThinkingConfig
from base.lm.registry import (
    attach_modalities_for_model as attach_modalities_for_model,  # re-exported resolution
)
from base.lm.registry import (
    media_types_for_model as media_types_for_model,  # re-exported resolution
)
from base.lm.registry import (
    resolve_available_model,
    resolve_setting,
)


class _LLMFactory(Protocol):
    """Contract for the callable that `AVA_LLM_OVERRIDE=mod:factory` points to.

    Newly written fake factories should conform to this signature: take
    model name and explicit agent id (None outside an agent), return a
    BaseChatModel subclass. `_resolve_override` runs
    isinstance(BaseChatModel) validation at the end; bad factories blow up
    at build time rather than crashing deep in the graph.
    """

    def __call__(self, model: str, *, agent_id: int | None) -> BaseChatModel: ...


def model_supports_vision(model: str, *, catalog: ModelCatalog) -> bool:
    """Whether `model` accepts images via registry media types, plugin vision, or fallback.

    Answers from the model's **raw registry entry**: a withdrawn id keeps its
    declared facts here, so callers judging an agent's effective model must
    resolve the withdrawal fallback first (`resolve_available_model`)."""
    return "image" in media_types_for_model(
        model,
        models=catalog.models,
        vision_prefixes={prefix: binding.vision for prefix, binding in catalog.bindings.items()},
    )


def vision_capable_provider_names(*, catalog: ModelCatalog) -> list[str]:
    """Display names of every vision-capable binding.

    Feeds the message endpoint's 422 error text (gateway/agents/
    state.py), so the "switch to a vision-capable model" hint stops
    being a hardcoded list that a new provider must remember to edit.
    """
    return [binding.display_name for binding in catalog.bindings.values() if binding.vision]


def provider_key_of_model(model: str, *, catalog: ModelCatalog) -> str | None:
    """Provider key for a model name, or None for an unregistered prefix.

    Each registered plugin's explicit provider key or stripped dispatch
    prefix. None means the model id matches no registered plugin.
    """
    return catalog.provider_key_of(model)


def provider_key_map(*, catalog: ModelCatalog) -> dict[str, tuple[str, str]]:
    """Provider dispatch prefix/key → (display name, key env var).

    The single source for `_ensure_provider_key`. The key lives in the process environment only (bootstrap
    plugin-secrets section on a split runner; the cluster `.env` file at the
    spawn boundary).
    """
    return {
        binding.provider_key or prefix: (binding.display_name, binding.key_env)
        for prefix, binding in catalog.bindings.items()
    }


def validate_model_config(
    *,
    model: str | None = None,
    config: dict[str, object] | None = None,
    check_provider_key: bool = True,
    catalog: ModelCatalog,
    llm_override: str,
) -> str:
    """Validate the selected model, its explicit effort and launch credentials.

    Called at the spawn boundary (gateway POST /api/agents handler) to fail
    fast before forwarding to the runner — a 400 with a clear message is
    better than an agent process starting and silently hanging.

    Resolves the effective model from ``config.llm_model`` first, falling back
    to ``model`` (the cluster default), then checks:
    1. The model name is a spawnable model of the catalog.
    2. The required API key is configured when ``check_provider_key`` is true.
    3. An explicit effort is one of the selected model's declared options.

    Args:
        model: fallback model name (cluster default). Ignored when
            ``config["llm_model"]`` is set.
        config: per-agent config overlay, may contain ``llm_model``.
        check_provider_key: check launch credentials; false for config-only edits.

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

    effective_model = resolve_available_model(effective_model, models=catalog.models)

    # 1. Model must be registered.
    all_models: list[str] = [m for models in catalog.supported_models.values() for m in models]
    if effective_model not in all_models:
        raise ValueError(
            f"unknown model {effective_model!r}. Available models: " + ", ".join(sorted(all_models))
        )

    _validate_model_effort(effective_model, config, catalog=catalog)

    # 2. API key must be configured — unless an LLM override is active
    # (e2e tests inject fake chat models via AVA_LLM_OVERRIDE and don't need
    # real keys; the override path in build_chat_model skips the real LLM).
    if not check_provider_key or llm_override:
        return effective_model

    _ensure_provider_key(effective_model, catalog=catalog)
    return effective_model


def _validate_model_effort(
    model: str, config: dict[str, object] | None, *, catalog: ModelCatalog
) -> None:
    """Reject unsupported explicit effort before spawn or override dispatch."""
    effort = config.get("reasoning_effort") if config is not None else None
    if effort is None or effort == "":
        return
    if not isinstance(effort, str):
        # Spawn handlers translate ValueError into an HTTP 400 response.
        raise ValueError("reasoning_effort must be a string")  # noqa: TRY004
    spec = catalog.models[model]
    validate_effort(effort, spec.effort_levels or (), target=model)


def _ensure_provider_key(effective_model: str, *, catalog: ModelCatalog) -> None:
    """Fail fast when the effective model's provider API key is not configured.

    Drives the lookup from `provider_key_map()`. The key has no Settings field, so it
    is read from the process env and the `.env` file directly. An unregistered model
    that slipped past the spawnable-model check raises.
    """
    for prefix, (_display, env_var) in provider_key_map(catalog=catalog).items():
        if not effective_model.startswith(prefix):
            continue
        # The key arrives on this unit's effective channel: a pure agent-runner
        # receives it from /api/bootstrap injected into os.environ (no
        # materialized .env cache of cluster facts since 2026-08-01), the gateway
        # loads its own .env into the process env at boot. The file
        # fallback covers the gateway profile that pops provider keys
        # from os.environ (Task #856 / regression #1562).
        from base.host.env.runtime_config import read_env_aliases

        if provider_api.provider_key_present(env_var) or env_var in read_env_aliases():
            return
        raise ValueError(
            f"{effective_model} requires {env_var} which is not "
            "configured on this unit — set it in the cluster .env "
            "(gateway unit) or restart the runner daemon so its "
            "bootstrap fetch delivers it"
        )
    raise ValueError(
        f"no provider mapping for model {effective_model!r} — "
        "enable the provider plugin that owns its prefix"
    )


def _resolve_override(override: str, model: str, *, agent_id: int | None = None) -> BaseChatModel:
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
    from langchain_core.language_models.chat_models import BaseChatModel

    result = factory(model, agent_id=agent_id)
    if not isinstance(result, BaseChatModel):
        raise TypeError(
            f"AVA_LLM_OVERRIDE={override!r}: factory returned {type(result).__name__!r}, "
            f"not a BaseChatModel subclass"
        )
    return result


def build_chat_model(
    model: str,
    *,
    agent_id: int | None = None,
    thinking: ThinkingConfig | None = None,
    reasoning_effort: str | None = None,
    streaming: bool | None = None,
    timeout: float | None = None,
    media_resolution: str | None = None,
    media_thinking_level: str | None = None,
    base_url: str | None = None,
    overrides: ModelOverrides,
    single_attempt: bool = False,
    catalog: ModelCatalog,
    llm_override: str,
) -> BaseChatModel:
    """Pick the provider by model name prefix and return the corresponding ChatModel.

    The returned ChatModel does not have tools bound — the caller binds them
    at use site via `llm.bind_tools([execute_code])`. This way paths that
    don't need tools (compaction etc.) can use the same ChatModel instance.

    Dispatches to a registered provider-plugin binding (each documents its own
    key / thinking / reasoning-effort wiring). The
    cross-provider resolution shared by every branch happens here:
    `AVA_LLM_OVERRIDE`, the streaming default, and the reasoning-effort knob
    (`resolved_effort` — explicit env/.env/overlay value wins, else the model's
    registry default, else the provider default).

    Args:
        model: e.g. `claude-sonnet-5` / `deepseek-flash`.
        agent_id: explicit owner for an override factory. None for non-agent
            callers; real provider construction does not use this metadata.
        thinking: cross-provider thinking switch (Anthropic Messages API
            shape). `{"type": "disabled"}` turns reasoning off where the
            provider supports it (short-text paths like label generation —
            thinking is slow/expensive and turns content into list-of-blocks);
            it also skips reasoning-effort injection where both would conflict
            (deepseek 400) or contradict intent (claude / glm / gemini /
            mimo / qwen). kimi-k3 cannot disable reasoning — logged
            and ignored. `{"type": "enabled", "budget_tokens": N}` is manual
            extended thinking — see the claude helper for the per-model rules.
            None = provider default.
        reasoning_effort: overrides the resolved effort for this call. Graded
            values must be exact members of the model's `effort_levels`.
            Provider-specific thinking switches retain their native on/off
            conversion; unsupported grades never change to another grade.
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

        media_resolution: media-path only (ava.understand) — the Gemini
            low/medium/high resolution setting, mapped onto Google's
            MediaResolution enum by the gemini branch; ignored by other
            providers. None = the SDK default.
        media_thinking_level: media-path only (ava.understand) — the Gemini
            thinking_level vocabulary carried explicitly (the media path maps
            `effort` itself, including the `max` → configured-knob special
            case) instead of the resolved cross-provider effort; also keeps
            include_thoughts at the SDK default, matching the media path's
            historic wire shape. Ignored by other providers.
        base_url: media-path only (ava.understand) — endpoint override for
            the gemini branch (e.g. a self-hosted relay / provider mirror);
            None = the SDK default official endpoint. Ignored by other
            providers.
        overrides: the agent's explicit tuning values (its slices' `overrides`): the
            reasoning effort and the thinking budget the model is built with. None =
            the cluster's, for a build that does not serve one agent.

    Raises:
        ValueError: model prefix did not match — prompts to add a branch.
        RuntimeError: a provider path needs its API key env; if missing,
            blows up immediately rather than reaching a server 401.
    """
    return build_chat_model_bound(
        model,
        agent_id=agent_id,
        thinking=thinking,
        reasoning_effort=reasoning_effort,
        streaming=streaming,
        timeout=timeout,
        media_resolution=media_resolution,
        media_thinking_level=media_thinking_level,
        base_url=base_url,
        overrides=overrides,
        single_attempt=single_attempt,
        catalog=catalog,
        llm_override=llm_override,
    )[0]


def _provider_model_ids(catalog: ModelCatalog, prefix: str) -> tuple[str, ...]:
    """The binding's registered IDs, retained in explicit validation diagnostics."""
    return tuple(sorted(name for name in catalog.models if name.startswith(prefix)))


def build_chat_model_bound(
    model: str,
    *,
    agent_id: int | None = None,
    thinking: ThinkingConfig | None = None,
    reasoning_effort: str | None = None,
    streaming: bool | None = None,
    timeout: float | None = None,
    media_resolution: str | None = None,
    media_thinking_level: str | None = None,
    base_url: str | None = None,
    overrides: ModelOverrides,
    single_attempt: bool = False,
    catalog: ModelCatalog,
    llm_override: str,
) -> tuple[BaseChatModel, provider_api.ProviderBinding | None]:
    """Internal companion: return the client and binding selected by this build."""
    # e2e tests inject fake chat model via AVA_LLM_OVERRIDE (tests/e2e/README.md);
    # if set, warn loudly — a dev accidentally leaving it in .env would route
    # all agents through a fake LLM, and production observability must be fail-loud.
    if type(single_attempt) is not bool:
        raise ValueError("single_attempt must be a boolean")
    override = llm_override
    if override:
        if single_attempt:
            raise ValueError("LLM overrides do not declare single-attempt construction")
        logger.warning(
            f"AVA_LLM_OVERRIDE active: model={model!r} does not go through real LLM, routed via {override!r}"
        )
        return _resolve_override(override, model, agent_id=agent_id), None

    # The composition root supplies a complete catalog, including for services
    # that never load agent-side plugin.py.

    requested_model = model
    model = resolve_available_model(model, models=catalog.models)
    if model != requested_model:
        if single_attempt:
            raise ValueError("single-attempt generation cannot change its frozen model")
        logger.warning(
            f"{requested_model} is temporarily unavailable; using its registered fallback {model}"
        )

    # Resolve streaming: explicit kwarg overrides model default, model default
    # overrides the fallback True (kimi defaults to True — streaming-first with
    # a non-streaming fallback on 429).
    spec = catalog.models.get(model)
    if streaming is None:
        streaming = spec.streaming if spec is not None else True
    disable_streaming = not streaming

    # The cross-provider reasoning-effort knob, resolved per model: explicit
    # env/.env/overlay value wins, else the model's registry default, else "".
    resolved_effort: str = resolve_setting(
        "reasoning_effort", model=model, models=catalog.models, explicit=overrides.reasoning_effort
    )

    # The prefix map is flat (no nesting, collisions rejected at registration),
    # so at most one binding matches. Every builder receives the shared
    # cross-provider context defined in provider_api.py.
    for prefix, binding in catalog.bindings.items():
        if model.startswith(prefix):
            builder = binding.build_single_attempt if single_attempt else binding.build
            if builder is None:
                raise ValueError("provider does not declare single-attempt construction")
            client = builder(
                provider_api.BuildContext(
                    model=model,
                    spec=spec,
                    provider_model_ids=_provider_model_ids(catalog, prefix),
                    thinking=thinking,
                    resolved_effort=reasoning_effort or resolved_effort,
                    disable_streaming=disable_streaming,
                    timeout=timeout,
                    effort_levels=binding.effort_levels,
                    media_resolution=media_resolution,
                    media_thinking_level=media_thinking_level,
                    base_url=base_url,
                    overrides=overrides,
                    thinking_budget_tokens=resolve_setting(
                        "claude_thinking_budget_tokens",
                        model=model,
                        models=catalog.models,
                        explicit=overrides.claude_thinking_budget_tokens,
                    ),
                )
            )

            return client, binding

    raise ValueError(
        f"Unknown model {model!r} — add a {model.split('-', maxsplit=1)[0]}-* "
        "provider plugin (base/lm/provider_api.py)"
    )
