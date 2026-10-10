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
[`docs/conventions/non-goals.md`](../../../docs/conventions/non-goals.md) and
[`docs/decisions/engineering/design/simplification/2026-07-29-no-runtime-model-routing.md`](../../../docs/decisions/engineering/design/simplification/2026-07-29-no-runtime-model-routing.md).

The extension surface enforces that boundary:

- A composition root builds its catalog at startup, outside the turn loop.
- `build(ctx)` receives construction inputs, not the caller, agent, error
  history, budget, or a list of fallback candidates.
- The prefix map is flat. Duplicate or nested prefixes fail at registration;
  there is no precedence order from which a fallback chain could emerge.

## Model invocation

`build_chat_model()` returns an unbound chat model; the internal
`build_chat_model_bound()` companion also returns the selected provider binding.
Agent streaming and compaction bind `execute_code` at the call site and send
the complete message prefix, including the original SystemMessage. Gemini
uses the provider's implicit caching through the ordinary model API.

Core owns total deadlines, cancellation, stream-stall recovery and usage
accounting. It has no explicit CachedContent lifecycle or cache-stale retry.
Compaction invokes once per caller attempt; guarded generation additionally
requires the provider's single-attempt construction contract.

## Current ownership

Core owns only the extension and normalization mechanisms:

- `catalog/provider_contract.py` owns the lightweight `ProviderBinding`, `BuildContext`,
  `AttachPolicy`, `PriceRates`, and fail-fast registration declaration types.
  `provider_api.py` exposes the same objects to plugins alongside key and model
  helpers; SDK imports consume the declaration owner without loading this API.
- `plugin_providers.py` discovers enabled plugins and imports their
  `provider.py` modules into a fresh builder for each explicit catalog.
  An Installation or service root retains that immutable result.
- `registry.py`, `factory.py`, `effort.py`, and `stop.py` assemble plugin data
  into provider-agnostic views and behavior using the supplied catalog.
  They do not retain a module-wide provider image.
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
separately loaded base-layer module. It may import `base` and installed
LangChain packages, never `ava` or `agent`, because gateway, labeler, and eval
processes load it without an agent runtime.

`build_model_catalog()` reuses `discover_plugins()` and `load_for_runtime()`,
imports enabled provider faces in sorted-name order, and returns a complete
immutable value. It retains no process catalog slot. The default configuration
enables all discovered providers, including the eight repository `lm_*` plugins.
Zero bindings raises before boot completes; correcting configuration and calling
the builder again starts a clean attempt.

Gateway, service, CLI and SDK installation roots retain that value and pass its
views to their consumers. Model resolution, pricing, media selection and DTO
admission never reconstruct an owner. Known declaration errors preserve typed
registration failures; unexpected Python errors retain their original object
and traceback. No failed attempt publishes a partial catalog.

## Provider contract

A `provider.py` declares one `ProviderContribution(binding, models, pricing)` (returned from `contribute()`) and each catalog construction installs it into its own builder:

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
- Installation validates prefix ownership, model-prefix/provider agreement,
  spawnable facts, current prices, effort defaults, and Anthropic-protocol
  output caps before the binding becomes available.

The current Sonnet and Sol successors are `claude-sonnet-5-5` and
`gpt-6.1-sol`. Their predecessors remain spawnable for existing agent
configurations and are hidden only from the spawn picker. Sonnet 5.5 rejects
`thinking.type=disabled`, so the Claude builder ignores that request and uses
adaptive thinking. GPT-6.1 Sol rejects explicit `none` effort. Graded effort
must exactly match the selected model's declared options; unsupported grades
fail at spawn and construction boundaries. Internal binary thinking-disable
requests use the lowest supported GPT effort when disabling is unavailable.

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
that runtime never loads; `load_archive` reads `pricing_catalog_archive.json`.
Runtime never scrapes a pricing page.

Future effective boundaries stay explicit in both the archive and generated
plugin declarations. The bot reports upcoming changes, and runtime switches at
the declared instant without waiting for another bot run.

## Boundary rationale

The plugin/core criterion is that deployment-physics extension points stay in
core while removable vendor bindings live in plugins
([`docs/decisions/extensions/plugins/2026-07-19-plugin-core-boundary-wrapper-extension.md`](../../../docs/decisions/extensions/plugins/2026-07-19-plugin-core-boundary-wrapper-extension.md)).
A binding's lifetime follows a vendor endpoint and the deployment's decision to
enable it; the registry, loader, normalization, and fail-fast invariants remain
useful regardless of which providers are installed.

The provider module is separate from the agent-facing plugin SDK. `ava.extend`
and namespace registration operate above `base/lm` and load only in agent
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
  startup failure, all 29 model/vendor mappings, archive/plugin price
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
