# Model providers as plugins

> **Status: implemented.** Core registers no provider bindings or chat models.
> The eight repository providers live under `ava_builtins/plugins/lm_*`, and
> enabled provider plugins are the sole source of chat models, builders, API-key
> declarations, effort vocabularies, vision fallbacks, stop vocabularies, and
> current prices.

## Constraint: a provider plugin adds access, never routing

A provider plugin makes one vendor's models nameable. It never decides which
model an agent runs on. Model choice happens at spawn; no provider hook observes
a request and swaps models after a failure or according to cost or load. This is
the standing non-goal in
[`conventions/non-goals.md`](../../conventions/non-goals.md) and
[`decisions/2026-07-29-no-runtime-model-routing.md`](../../decisions/2026-07-29-no-runtime-model-routing.md).

The extension surface enforces that boundary:

- Registration happens once per process, outside the turn loop.
- `build(ctx)` receives construction inputs, not the caller, agent, error
  history, budget, or a list of fallback candidates.
- The prefix map is flat. Duplicate or nested prefixes fail at registration;
  there is no precedence order from which a fallback chain could emerge.

## Current ownership

Core owns only the extension and normalization mechanisms:

- `provider_api.py` defines `ProviderBinding`, `BuildContext`, `AttachPolicy`,
  `PriceRates`, and the fail-fast registration contract.
- `_plugin_providers.py` discovers enabled plugins and imports their
  `provider.py` modules under a process-wide lock.
- `registry.py`, `factory.py`, `_effort.py`, and `stop.py` assemble plugin data
  into provider-agnostic views and behavior. Their provider-owned tables start
  empty.
- `pricing.py` selects plugin runtime prices or catalog-only archive prices and
  preserves the retired-model ledger.

The repository ships this default enabled set:

| Plugin | Prefix | Client binding | Stop vocabulary owner |
|---|---|---|---|
| `lm_alibaba` | `qwen3.8-` | `ReasoningContentChatModel` | `lm_openai` (`openai`) |
| `lm_anthropic` | `claude-` | `ThinkingTokensChatAnthropic` | `lm_anthropic` (`anthropic`) |
| `lm_deepseek` | `deepseek-` | `ThinkingTokensChatAnthropic` | `lm_anthropic` (`anthropic`) |
| `lm_google` | `gemini-` | `ChatGoogleGenerativeAI` | `lm_google` (`google_genai`) |
| `lm_moonshot` | `kimi-` | `ChatMoonshot` | `lm_moonshot` (`moonshot`) |
| `lm_openai` | `gpt-` | `ChatOpenAI` Responses API | `lm_openai` (`openai`) |
| `lm_xiaomi` | `mimo-` | `ReasoningContentChatModel` | `lm_openai` (`openai`) |
| `lm_zhipu` | `glm-` | `ReasoningContentChatModel` | `lm_openai` (`openai`) |

Stop vocabulary is client-class scoped, not model-prefix scoped. The
`model_provider` value emitted by `ChatAnthropic` is `anthropic`, so the
Anthropic registration also classifies DeepSeek. `ReasoningContentChatModel`
subclasses `ChatOpenAI`, so the OpenAI registration also classifies Alibaba,
Xiaomi, and Zhipu. A binding that reuses one of those clients omits
`stop_spec`; a client with a distinct emitted key owns one registration.

## Loading and startup

Discovery is keyed on a plugin directory's `plugin.py`; `provider.py` is the
separately loaded shared-layer module. It may import `shared` and installed
LangChain packages, never `ava` or `agent`, because gateway, labeler, and eval
processes load it without an agent runtime.

`ensure_provider_plugins_loaded()` reuses `_discover_plugins()` and
`load_for_runtime()`, imports enabled `provider.py` files in sorted-name order,
and sets its once flag only after registration succeeds. The default config
enables every discovered plugin, including the exact eight `lm_*` plugins
above. A deployment that disables or loses every provider raises:

```text
no provider plugins enabled — enable at least one provider plugin (the repo ships the lm_* default set; check the plugin enable config)
```

That failure occurs before the loaded flag is set, so a corrected enable
configuration is retryable. Gateway lifespan calls the loader directly during
startup, outside best-effort blocks; a zero-provider deployment therefore
fails boot rather than serving an empty model list. Other consumers retain the
same loader guard at their first registry read.

Registration mutates `MODELS` and rebuilds `SUPPORTED_MODELS`,
`MODEL_CONTEXT_WINDOW`, `MODEL_KNOWLEDGE_CUTOFF`, and `MODEL_IDENTITY` in place.
Existing importers see a newly registered model immediately, with no module
reload or process restart. The known-provider-key cache is invalidated by the
same registration.

## Provider contract

A `provider.py` calls `register(binding, models=..., pricing=...)` once:

- `ProviderBinding` declares the dispatch prefix, display name, `.env` key,
  builder, provider-wide effort ladder, vision fallback, optional attachment
  policy, optional client-class `StopSpec`, and an optional stable provider-key
  override. Attachment policies keep provider-specific byte, image-dimension,
  and PDF wire-shape rules out of core; an absent policy uses core defaults.
- Each `ModelSpec` owns model-specific availability, context, output cap,
  knowledge cutoff, effort ladder, tuning defaults, identity, and media types.
- Each `PriceRates` owns the complete effective-period, input-tier, and daily-
  window pricing lattice plus official-source provenance and vendor vocabulary.
  Its flat fields remain the current base-tier shortcut for older plugins.
- Registration validates prefix ownership, model-prefix/provider agreement,
  spawnable facts, current prices, effort defaults, and Anthropic-protocol
  output caps before the binding becomes available.

`ProviderBinding.key_env` is the secret-delivery declaration. The gateway reads
the cluster `.env` during spawn validation, bootstrap relays enabled bindings'
present keys to split runners, and the single-box child allowlist forwards only
declared provider keys. Plugin config images never carry provider secrets.

The builder is plain Python deliberately: provider wire behavior is the place
where a closed schema becomes restrictive. It must return an unbound
`BaseChatModel`, fail immediately on a missing key, preserve each provider's
established effort behavior (including GPT's verbatim pass-through), and honor
the shared thinking switch according to provider capability.

## Pricing catalog and runtime prices

`pricing_catalog_archive.json` is the complete reconciliation ledger. It keeps
official provenance, historical effective periods, token tiers, recurring UTC
windows, and scheduled future price windows for all repository chat models and
catalog-priced services. `gemini-embedding-2` remains catalog-priced because it
has no chat `ProviderBinding`.

Provider `PriceRates` are the runtime source for chat models. When a plugin
registers a chat-model price, `pricing.py` removes the overlapping archive row
from its in-memory runtime catalog view and uses the plugin's complete lattice.
Both declarations pass through the same parser and selection code; the archive
remains available for independent equivalence tests and bot reconciliation.
`pricing_catalog.json` is retained only as an empty placeholder (`"models": {}`)
that runtime never loads; `_load_catalog` reads `pricing_catalog_archive.json`.
Runtime never scrapes a pricing page.

Future effective boundaries stay explicit in both the archive and generated
plugin declarations. The bot reports upcoming changes, and runtime switches at
the declared instant without waiting for another bot run.

## Boundary rationale

The plugin/core criterion is that deployment-physics extension points stay in
core while removable vendor bindings live in plugins
([`decisions/2026-07-19-plugin-core-boundary-wrapper-extension.md`](../../decisions/2026-07-19-plugin-core-boundary-wrapper-extension.md)).
A binding's lifetime follows a vendor endpoint and the deployment's decision to
enable it; the registry, loader, normalization, and fail-fast invariants remain
useful regardless of which providers are installed.

The provider module is separate from the agent-facing plugin SDK. `ava.extend`
and namespace registration operate above `shared/lm` and load only in agent
processes, so they cannot supply bindings to gateway validation, labeler, or
eval consumers. `AVA_LLM_OVERRIDE` is likewise only a test-injection seam: it
replaces the factory and deliberately skips real-key validation rather than
adding a provider.

## Resolved questions

- **Dependencies:** repository provider dependencies remain pinned in Ava's
  `pyproject.toml`. An external provider can use only packages already present
  in the Ava environment; provider plugins do not have an independent
  dependency-install mechanism.
- **Repository providers:** all eight current bindings are plugins. Core ships
  the contract and an all-enabled default set, not fallback providers.
- **Tests:** provider contract tests lock duplicate/nested-prefix rejection,
  immediate model visibility, the exact default plugin set, zero-provider
  startup failure, all 33 model/vendor mappings, archive/plugin price
  equivalence, stop-vocabulary ownership, and gateway model views.

## Alternatives rejected

- **Core patches for each vendor:** this couples endpoint-specific builders,
  keys, vocabularies, models, and prices to every deployment and defeats
  provider removability.
- **A runtime router or fallback list in the provider registry:** an ordered
  candidate set is routing by another name and would also invalidate stable
  provider prompt-cache prefixes during a run.
- **A closed schema for builders:** vendor integrations contain unanticipated
  wire behavior; plain Python behind a narrow documented context preserves the
  boundary without pretending every client can be configured identically.
- **`AVA_LLM_OVERRIDE` for real providers:** the override bypasses normal
  provider registration and key validation, which is correct for fakes and
  incorrect for production access.
