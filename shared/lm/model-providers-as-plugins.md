# Model providers as plugins

> **Status: nothing built.** Onboarding a model provider is a core edit today,
> spread across up to seven files — six of them under `shared/` — in this repo,
> for every vendor. This doc commits to making provider onboarding a plugin
> concern and names the exact extension points the mechanics must open. It does
> not open them.
>
> One thing is already true and worth keeping: downstream of the registry the
> roster is **data, not code**. Pricing, the spawn dropdown, context budgets and
> the per-model config view all derive from `shared/lm/registry.py:MODELS` and
> need no per-provider edit. What is still code is the **binding** — how the
> vendor's client is constructed — and that is what a plugin should own.

## The constraint: a provider plugin adds access, never routing

A provider plugin makes one more vendor's models *nameable*. It never decides
which model an agent runs on. That is the standing non-goal — no framework
mechanism swaps an agent's model mid-run to paper over a provider error, and
none picks one by an opaque cost/load heuristic
([`conventions/non-goals.md`](../../conventions/non-goals.md),
[`decisions/2026-07-29-no-runtime-model-routing.md`](../../decisions/2026-07-29-no-runtime-model-routing.md)).
Model choice is made once, at spawn, by whoever is deciding the agent's job.

That is not a disclaimer to bolt on afterwards; it is a shape constraint on the
extension surface itself. Three things fall out of it directly:

- **Registration happens at process start, never per turn.** There is no hook
  that runs inside a turn boundary, so no plugin can observe a request and
  answer it with a different model.
- **`build_chat_model(model)` stays a pure function of the model id.** A plugin
  supplies the builder for a prefix; it does not get to see the caller, the
  agent, the error history, or the budget.
- **The prefix map is flat, and a collision is an error.** Any *ordered* list of
  candidate providers for one model is a fallback chain wearing a different
  name, and would grow into the router this project has said no to. Two plugins
  claiming `foo-` must fail fast at load, not resolve by precedence.

The carve-out stays where it already lives: an ordered fallback chain becomes a
real *availability* mechanism once no operator is present to swap config, and is
tracked as an open-source prerequisite
([`future/roadmap/open-source-prerequisites.md`](../../future/roadmap/open-source-prerequisites.md),
"Provider fallback chain"). Nothing in this doc builds toward it, and a provider
plugin API shaped as above does not accidentally deliver it.

## What a provider costs today

Read off the roster as it stands (eight providers), file by file. The last real
vendor addition — `69514272` "feat: add Kimi K3, GLM 5.2, Grok 4.5 model support
(#485)", 2026-07-16 — is deliberately *not* cited as corroboration: it predates
`_effort.py` (2026-07-23), `registry.py` (2026-07-24) and `_providers.py`
(2026-07-31), so three of the files below did not exist when it landed. The cost
grew after that commit, not before it.

| Edit | Where | Why it is there |
|---|---|---|
| API key field | `shared/config/lm.py` — `<vendor>_api_key: SecretStr \| None` with its env alias | `scope: "cluster-pinned"` is what makes the key travel to a split agent-runner through `/api/bootstrap`; `sensitive: True` masks it in the config panel |
| Model facts | `shared/lm/registry.py` — one `ModelSpec` per model id in `MODELS` | `ModelSpec.provider` **is** the `build_chat_model` prefix and the `SUPPORTED_MODELS` group key. `_validate_registry()` refuses at import if a `spawnable` model lacks `context_window` / `knowledge_cutoff` / `pricing` / `effort_levels` |
| Client construction | `shared/lm/_providers.py` — a `_build_<vendor>_model(...)` helper | Key check, thinking switch, effort injection, streaming — the whole vendor-specific wire shape |
| Dispatch | `shared/lm/factory.py` — a `_MODEL_KEY_MAP` entry plus a prefix branch in `build_chat_model` | `_MODEL_KEY_MAP` is the single-source prefix → (display name, settings attr, env var) map that `_ensure_provider_key` also drives, so the spawn boundary can 400 on a missing key |
| Vision gate | `shared/lm/factory.py` — `_VISION_MODEL_PREFIXES`, only if the endpoint accepts images | The message endpoint 422s an image addressed to a text-only agent up front instead of letting the LLM call fail after the inbound is queued |
| Effort vocabulary | `shared/lm/_effort.py` — a `_PROVIDER_EFFORT_LEVELS` entry | OpenAI-style branches only; the claude branch clamps per model off `ModelSpec.effort_levels` instead |
| Terminal-reason vocabulary | `shared/lm/stop.py` — a `ProviderKey` member plus its `_BY_PROVIDER` spec (`stop.py:36`, `:52`) | LangChain standardizes tool calls and usage metadata but *not* the finish/stop reason, so `classify_stop` carries each vendor's key and word list — and raises on a `model_provider` it does not know, mid-turn |
| Dependency | `pyproject.toml` — the vendor's LangChain package | Skipped when the endpoint is OpenAI-compatible enough for `shared/lm/_reasoning_compat.py:ReasoningContentChatModel`; glm and mimo take that path and add no dependency |

The `stop.py` row is the one keyed by something other than the model prefix: its
key is the `model_provider` string the LangChain client emits, so five entries
already cover today's eight vendors — `anthropic` serves claude *and* deepseek
(deepseek binds `ChatAnthropic` against an anthropic-compatible endpoint), and
`openai` serves gpt, mimo *and* glm (`ReasoningContentChatModel` subclasses
`ChatOpenAI`). So it is a conditional cost: a vendor bound through a client class
already in that table costs nothing there; one shipping its own class costs an
entry, and skipping it turns into a `ValueError` on the first turn that ends.

Cosmetic and optional: `ui/web/src/lib/models.ts:PROVIDER_LABELS` prettifies a
provider key. An unrecognized provider still renders, just capitalized.

Everything else is already derived and needs no edit — this is the part worth
preserving through any refactor:

- `shared/lm/pricing.py:MODEL_PRICING`, `SUPPORTED_MODELS`,
  `MODEL_CONTEXT_WINDOW`, `MODEL_KNOWLEDGE_CUTOFF`, `MODEL_IDENTITY` — all
  comprehensions over `MODELS`.
- `GET /api/models` (`gateway/routers/agents.py:get_models`) serves the spawn
  dropdown straight from the registry, so the frontend picks a new provider up
  with no frontend change.
- `shared/lm/context_budget.py:resolve_context_budget` and the per-model config
  view (`gateway/routers/config.py`, via `registry.explain_setting`) likewise.

So the edit is small, but it is a **core** edit in this repo: a private provider
means carrying a diff across every `git pull`, and the vendor's SDK lands in the
shared `pyproject.toml` for every install whether or not that vendor is used.
That is the cost the plugin path removes.

## Why the existing plugin mechanism cannot carry it

The plugin system gives a plugin two ways to change the agent-facing SDK, and
they are the whole surface: `ava.extend.wrap(target, fn)` layers behavior over an
existing `ava.*` member (`ava/_extend.py`, decided in
[`decisions/2026-07-19-plugin-core-boundary-wrapper-extension.md`](../../decisions/2026-07-19-plugin-core-boundary-wrapper-extension.md)),
and `ava.register_namespace(name, module)` / `register_namespace_member(...)`
add a new one (`ava/_exports/plugins.py` — this is how `ava_memory` owns
`ava.memory` outright). Neither can reach the provider layer, for three
independent reasons that apply equally to both. Any one of them is
disqualifying.

1. **Import layering.** The contract is `shared < ava < agent < gateway <
   {cli}` (`pyproject.toml`, import-linter). Both primitives live in
   `ava`; `shared/lm/factory.py` is a layer below and cannot import them. A
   plugin's `plugin.py` sits higher still — it imports the agent runtime.
2. **Plugins load in the agent process only.** Loading is
   `agent/graph/_build.py:_load_extensions()`: it discovers builtin and external
   plugins through `shared/plugins_config.py:_discover_plugins()`, reads the
   enabled set, and imports each enabled `plugin.py` **itself**, by path, with
   `importlib.util.spec_from_file_location` — one loop covering both kinds
   (`ava_builtins.plugins.<n>.plugin` / `plugins.<n>.plugin`). `ava._extend`
   also exposes a simpler external-only loader, `scan_and_load()`, whose one
   call site is `agent/loop.py:527` at process boot. Both entry points sit in
   `agent`. But a chat model is built or validated in contexts that never load
   plugins at all: the gateway spawn boundary (`validate_model_config` behind
   `POST /api/agents`), the labeler daemon (`services/labeler/labeler.py`
   imports `build_chat_model` directly), and the eval harness
   (any embedding driver). A
   separate import-linter contract forbids `services` from importing `agent` at
   all, so the labeler cannot be fixed by loading agent plugins there.
3. **Wrong altitude.** Both primitives operate on the agent-facing SDK —
   `wrap` replaces one of its members, `register_namespace` adds one. Provider
   construction sits below the SDK entirely: the SDK's own consumers
   (`ava/web.py`, `ava/_understand.py`) call `build_chat_model` themselves, so a
   provider registered as an `ava.*` member would arrive after the code that
   needs it.

The conclusion this doc commits to: **the provider extension point is not
`plugin.py`, and it is neither of the `ava.*` primitives.** It belongs at the
`shared` layer, and it must be loadable by any process that builds a chat model.

## The shape already exists: a `shared`-only plugin module

A plugin today is a directory carrying up to three separately-loaded modules,
plus bundled assets:

| Module | Loaded by | May depend on |
|---|---|---|
| `plugin.py` | `agent/graph/_build.py:_load_extensions()` in the agent process | the agent runtime, hooks, the `ava.*` namespace |
| `setup.py` | `cli/commands/_converge_plugins.py:run_plugin_scaffolds` on every converge | **`shared` only** |
| `default_config.py` | `shared/plugins_config.py:update_all_disk_images`, and the framework extension load via `shared/plugin_config_registry.py:register_plugin_config` | `shared` only |
| bundled `skills/`, `.mcp.json` | converge / the MCP loader | n/a |

The second and third rows are the precedent, and they are not an accident. Both
modules exist *separately from* `plugin.py` precisely because `plugin.py` drags
in an agent runtime the loading process does not have:
`cli/commands/_converge_plugins.py` says a scaffold "may depend on `shared`
only", and `update_all_disk_images` documents that it imports `default_config.py`
and "not plugin.py" to avoid triggering hook and state registration. Both load
their module standalone by path with
`importlib.util.spec_from_file_location`, gated on the same enabled set
(`shared/plugins_config.py:_discover_plugins` + `load`).

The `default_config.py` path is the stronger precedent: that loader lives **in
`shared/` itself**. So a `shared`-layer module loading a plugin-supplied module,
without importing `ava` or `agent`, is a thing this codebase already does in
production — the provider extension point does not need a layering exception, a
new discovery mechanism, or a new enabled-set concept.

A provider binding should therefore be a fourth module of the same family — call
it `provider.py` — discovered the same way, importable from `shared/lm`.

## The extension points

What the mechanics work must open, in dependency order.

1. **`shared/lm/registry.py` — a registration entry for `MODELS`.** A
   `register_models(...)` that merges `ModelSpec` entries under a plugin's
   provider key, and rejects a duplicate model id. The real mechanical work is
   that `SUPPORTED_MODELS`, `MODEL_CONTEXT_WINDOW`, `MODEL_KNOWLEDGE_CUTOFF` and
   `MODEL_IDENTITY` are module-level constants computed once at import: they
   have to become recomputed views (or callables) or a plugin's models will be
   invisible to every consumer that reads them. `_validate_registry()` must run
   over registered entries too — a plugin model missing `pricing` should fail at
   registration, not surface as an unpriced eval.
2. **`shared/lm/factory.py` — prefix dispatch as a map.** The `if
   model.startswith(...)` chain in `build_chat_model` becomes a prefix →
   builder lookup that a plugin adds to, and `_MODEL_KEY_MAP` gains the same
   registration path so `_ensure_provider_key` keeps working at the spawn
   boundary. `_VISION_MODEL_PREFIXES` is the third table on this file keyed by
   prefix and needs the same treatment. Registration is keyed and flat;
   a duplicate prefix raises.
3. **The per-provider vocabularies — `shared/lm/_effort.py`
   (`_PROVIDER_EFFORT_LEVELS`) and `shared/lm/stop.py` (`_BY_PROVIDER`).** Both
   are endpoint contracts rather than model facts, so both register alongside
   the builder. Unknown effort strings must keep failing fast at build time
   rather than arriving as a provider 400 mid-run; the stop vocabulary is the
   conditional one (a plugin binding an existing client class inherits an
   existing key), but leaving it unregisterable means a plugin that ships its
   own client class cannot complete a single turn.
4. **The API key.** This is the open one. Today a provider key is a framework
   `Settings` field whose `scope: "cluster-pinned"` puts it in `BOOTSTRAP_FIELDS`,
   which is how it reaches a split agent-runner through `/api/bootstrap`
   (`shared/config/__init__.py:bootstrap_config_values`). Plugin config
   (`shared/plugin_config_registry.py`) is a different channel: a per-plugin
   JSON image under `~/.ava/configs/<plugin>/config.json`, with no scope axis and
   no bootstrap distribution — and writing a vendor API key into a plaintext disk
   image is its own decision. Either plugin config grows a secret/scope class, or
   a plugin-declared key registers into the existing `.env` + bootstrap surface.
   Not decided here.
5. **The load call.** Whoever registers must run before the first
   `build_chat_model` / `validate_model_config` in *each* process that has one —
   agent, gateway, labeler, evals. A lazy load inside the factory (first call
   loads enabled provider modules once) keeps every entry point honest without
   asking four different mains to remember.

## Boundary check against the plugin/core criterion

The standing criterion is that capabilities which die as models improve live in
plugins, while things tied to deployment physics — including "the extension
points themselves" — live in core
([`decisions/2026-07-19-plugin-core-boundary-wrapper-extension.md`](../../decisions/2026-07-19-plugin-core-boundary-wrapper-extension.md)).
A vendor binding does not obviously die as models improve, so the criterion is
worth applying carefully rather than assumed.

It lands cleanly, and the split is the one the criterion already names: the
**dispatch and the registry contract stay in core** (they are the extension
points), while each **vendor binding becomes removable** — its lifetime is tied
to that vendor's endpoint existing and being wanted on this deployment, not to
model capability. Disabling a provider plugin should leave no trace: no
dependency, no config field, no dropdown entry. That is the same removability
test every other plugin is held to.

The eight bindings in `shared/lm/_providers.py` today are not automatically
candidates for extraction. Deciding which (if any) move out of core is a
separate call, taken when the mechanics exist — a repo that ships zero providers
by default is a different product decision from a repo that ships eight and
allows a ninth.

## Alternatives rejected

- **Keep onboarding a provider as a core patch (the status quo).** Rejected as
  the *end state*, not as a description of today. The table above is the cost:
  every vendor widens the core dispatch surface — a factory branch, a key field,
  an effort table, sometimes a stop-reason table — which is the opposite of the
  small-core minimal design, and a vendor binding is exactly the kind of
  thing whose lifetime is tied to an endpoint existing rather than to model
  capability. The seams are named here only because they are where a plugin
  hooks; the commitment is that a *new* provider stops editing them.
- **Let the plugin machinery carry a runtime router or fallback chain.**
  Rejected, and this is the load-bearing one — see the constraint section above,
  which shapes the extension surface so the router cannot be smuggled in through
  registration. Beyond the standing non-goal there is a mechanical reason a
  router does not pay for itself: every provider's prompt cache is keyed to a
  stable system-prompt + tool-schema prefix on one model, so a mid-run swap
  invalidates the prefix and spends the savings back on cache misses
  ([`2026-07-29-no-runtime-model-routing.md`](../../decisions/2026-07-29-no-runtime-model-routing.md)).
  The open-source-scale availability carve-out stays gated on its roadmap entry
  and is untouched by this doc.
- **A schema'd hook registry that providers declare themselves into.** Rejected
  for the reason the plugin boundary decision already gives generally
  ([`2026-07-19-plugin-core-boundary-wrapper-extension.md`](../../decisions/2026-07-19-plugin-core-boundary-wrapper-extension.md)):
  a schema admits only the shapes it anticipated, and a vendor binding is
  precisely where the unanticipated lives — a thinking switch here, a
  `reasoning_content` recovery there, a conversation-id namespace for grok. The
  builder stays plain Python behind a documented contract, which is also what
  keeps it code-as-action rather than configuration.
- **`AVA_LLM_OVERRIDE` as the onboarding path.** It already resolves a
  `module:factory` string to a `BaseChatModel` at build time, which looks like
  the registration hook this doc is asking for. It is not one: it is the e2e /
  multi-instance **test injection** seam — it replaces the factory wholesale
  rather than adding a provider to it, and `validate_model_config` returns early
  without calling `_ensure_provider_key` whenever it is set, so the key check is
  deliberately skipped: correct for a fake, wrong for a real vendor. The
  `_LLMFactory` protocol in `shared/lm/factory.py` documents that injection
  contract specifically; it is not the per-provider builder contract, and a
  provider extension point should not be built by widening it.

## Open

- The API key channel (extension point 4) — the one real blocker.
- Whether a provider plugin may ship its own dependency, and how it gets
  installed. `pyproject.toml` is the repo's; a plugin that needs
  `langchain-<vendor>` has nowhere to declare it today.
- Whether any of the eight current bindings move out of core, or whether the
  extension points exist only for third-party providers.
- Test story: `tests/agent/test_llm_factory.py` enumerates providers directly.
  A registration path needs its own coverage for duplicate-prefix rejection and
  for a plugin model reaching `GET /api/models`.
