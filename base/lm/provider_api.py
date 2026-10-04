"""Provider plugin contract — what a plugin's ``provider.py`` declares.

A provider plugin makes one more vendor's models *nameable*. It never decides
which model an agent runs on — no routing, no fallback, no per-turn hook
(``base/lm/model-providers-as-plugins.md``,
``decisions/2026-07-29-no-runtime-model-routing.md``). A ``provider.py`` registers nothing: it
exports ``contribute()`` returning a ``PluginContributions`` whose ``providers`` hold one
``ProviderContribution`` each (the binding, the model rows and the prices). The process's model
catalog (``base/lm/catalog.py``) is built by ``base/lm/plugin_providers.py``, which loads every
enabled plugin's ``provider.py`` once per process, before the first build / spawn validation /
model list, and installs each declaration into the catalog builder; the prefix map is flat — a
duplicate prefix, or one that nests inside another (``foo-`` vs ``foo-bar-``), fails fast at
installation.
Core registers no providers; enabled plugins are the sole source of bindings,
chat-model rows, provider vocabularies, media fallbacks, keys, and live prices.

A ``provider.py`` module may import ``base`` and LangChain packages only —
never ``ava`` / ``agent`` (the gateway, the labeler daemon, and the eval
harness load it, and none of those processes has an agent runtime). Plugin
discovery is keyed on ``plugin.py``, so a provider plugin ships one even when
it contributes nothing agent-side (an empty stub satisfies discovery and the
enable switch).

``ProviderBinding.key_env`` declares the key's `.env` delivery channel: the
gateway reads the file at spawn validation, bootstrap relays enabled bindings'
present keys to split runners, and every agent process and exec child reads the key
from its own boot of that channel. Plugin config images do not carry provider secrets.

Builder contract (plain Python, documented rather than schema'd — see
``decisions/2026-07-19-plugin-core-boundary-wrapper-extension.md``):

- ``build(ctx)`` returns a ``BaseChatModel`` with no tools bound (the caller
  binds them). It is a pure function of ``ctx`` — no caller, agent, error
  history, or budget is visible.
- Missing API key raises ``RuntimeError`` immediately (``require_key``) — a
  clear build-time error, not a server 401 mid-turn.
- ``ctx.resolved_effort`` keeps the provider's established wire semantics.
  Builders with a constrained vocabulary clamp with
  ``base.lm.effort.clamp_effort``; the GPT builder preserves the resolved
  cross-provider value verbatim.
- ``thinking={"type": "disabled"}`` is honored per provider capability
  (mirror onto the local switch, or log-and-ignore like the Moonshot plugin)
  — the core dispatch resolves the cross-provider knobs first;
  the builder owns only the wire shape.
- A builder wrapping ``ChatAnthropic`` must set ``anthropic_protocol=True``
  so registration validates ``max_output_tokens`` on every spawnable model
  (langchain-anthropic falls back to a legacy 4096 for unknown ids — #169).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, Literal, NamedTuple, NotRequired, TypedDict

if TYPE_CHECKING:
    # Annotation-only reference (`ProviderBinding.build`): the registration
    # path must not import the LangChain chat-model stack (exec-child boot,
    # task #3633; `_TYPE_CHECKING_ALLOWED`: heavy dependency on a path that
    # does not use the type).
    from langchain_core.language_models.chat_models import BaseChatModel

from base.host.env.agent_slices import ModelOverrides
from base.lm.registry import ModelSpec
from base.lm.stop import StopSpec

# Increment for breaking contract changes; additive optional fields stay on the
# current version. A plugin written against an older shape keeps working —
# consumers of a new field must degrade when it is absent. (Mirrors the
# plugin-spec-v2 ``engines.ava`` host-compatibility idea.)
PROVIDER_API_VERSION = 2


class ThinkingConfig(TypedDict):
    """The Anthropic extended-thinking config passed to `build_chat_model`.

    `{"type": "disabled"}` turns thinking off (short-text paths); `{"type":
    "enabled", "budget_tokens": N}` turns it on with a token budget;
    `{"type": "adaptive"}` (adaptive-thinking claude models only) lets the
    model decide whether to think, with `display` choosing summarized text
    vs signature-only. Only the claude / deepseek branches pass the dict
    through on the wire; every other branch reads `type` and mirrors
    disabled onto its own switch (the provider plugins own those switches);
    kimi logs a warning instead.
    """

    type: Literal["enabled", "disabled", "adaptive"]
    budget_tokens: NotRequired[int]
    display: NotRequired[Literal["summarized", "omitted"]]


class PriceWindow(NamedTuple):
    """One recurring UTC rate override within a token tier."""

    start: str
    end: str
    cache_miss: str | Decimal
    cache_hit: str | Decimal
    output: str | Decimal


class PriceTier(NamedTuple):
    """One gapless input-token band and its optional daily overrides."""

    input_tokens_min: int
    input_tokens_max: int | None
    cache_miss: str | Decimal
    cache_hit: str | Decimal
    output: str | Decimal
    windows: tuple[PriceWindow, ...] = ()


class PricePeriod(NamedTuple):
    """One half-open effective interval containing all input-token tiers."""

    effective_from: str | None
    effective_until: str | None
    tiers: tuple[PriceTier, ...]


@dataclass(frozen=True)
class PriceRates:
    """One model's complete price and vendor declaration (USD per 1M tokens).

    Plugins declare prices in code — the plugin is itself the reviewed object
    (``decisions/2026-07-29-skill-trust-tiers-and-install-scan.md``). The
    flat fields are a readable shortcut for one unbounded base tier. ``periods``
    carries history, tiers, and recurring windows when present; its shape mirrors
    ``base/lm/pricing_catalog_archive.json`` so runtime and archive selection
    share one parser (``decisions/2026-08-18-versioned-model-pricing-catalog.md``).
    """

    cache_miss: float  # input tokens not served from cache
    cache_hit: float  # cache-read input tokens
    output: float
    source_url: str  # HTTPS official pricing page
    source_checked_at: str  # YYYY-MM-DD
    vendor: str | None = None  # stable billing vocabulary; absent for older plugins
    periods: tuple[PricePeriod, ...] = ()


@dataclass(frozen=True)
class BuildContext:
    """Construction inputs, including optional effort and media-provider knobs."""

    model: str
    spec: ModelSpec | None  # the registered ModelSpec for `model` (None = unregistered id)
    thinking: ThinkingConfig | None  # caller-passed thinking switch (Anthropic shape)
    resolved_effort: str  # explicit env/overlay wins, else per-model default, else ""
    disable_streaming: bool
    timeout: float | None
    effort_levels: tuple[str, ...] | None = None
    media_resolution: str | None = None
    media_thinking_level: str | None = None
    base_url: str | None = None
    # The agent's explicit tuning values; a builder resolves its per-agent settings through them.
    overrides: ModelOverrides | None = None


def _empty_file_size_limits() -> dict[str, int]:
    return {}


@dataclass(frozen=True)
class AttachPolicy:
    """Provider-owned limits and wire shape for local file attachments.

    ``file_size_limits`` narrows the core ``ATTACH_MAX_FILE_BYTES`` ceiling
    for a media type; entries above that ceiling are inert while it stands.
    """

    file_size_limits: Mapping[str, int] = field(default_factory=_empty_file_size_limits)
    image_dimension_tiers: tuple[tuple[int, int], ...] = ()
    pdf_document_block: bool = False


@dataclass(frozen=True)
class ProviderBinding:
    """One dispatch prefix's client binding and optional provider-key override."""

    prefix: str  # e.g. "foo-"; dispatch is `model.startswith(prefix)`
    display_name: str  # human-facing provider name (errors, UI text)
    key_env: str  # the API key env var, e.g. "FOO_API_KEY"
    build: Callable[[BuildContext], BaseChatModel]
    effort_levels: tuple[str, ...] | None = None
    vision: bool = False  # the bound endpoint accepts native image content blocks
    anthropic_protocol: bool = False  # ChatAnthropic binding — see module docstring
    stop_spec: StopSpec | None = None  # set by the plugin that owns the client
    # class's emitted model_provider string; compatible bindings share its entry
    # Usually derived from prefix.rstrip("-"); set only when a narrower legal
    # dispatch prefix differs from the stable public provider identity.
    provider_key: str | None = None
    # Absent means the core attachment defaults; older plugins and consumers
    # degrade without a provider-specific override.
    attach: AttachPolicy | None = None


def provider_key_present(key_env: str) -> bool:
    """Whether the live process env carries ``key_env``.

    The env half of the spawn-boundary channel (``_ensure_provider_key``):
    a pure agent-runner receives provider keys from ``/api/bootstrap``
    injected into os.environ (no materialized .env cluster facts since
    2026-08-01); the gateway loads its own ``.env`` into the process env at
    boot. Callers combine this with the ``.env``-file fallback.
    """
    import os

    return bool(os.environ.get(key_env))


def require_key(key_env: str) -> str:
    """Read a plugin provider's API key from the process environment, failing
    fast with the same posture as the core builders.

    The spawn-boundary check (``_ensure_provider_key``) reads os.environ
    first, then the unit's ``.env`` file fallback; a pure agent-runner
    receives the key through ``/api/bootstrap`` (plugin-secrets section) into
    ``os.environ`` before any build. This helper is the build-time half of
    the same channel.
    """
    import os

    key = os.environ.get(key_env)
    if not key:
        raise RuntimeError(
            f"{key_env} not set — this provider needs its key; "
            "configure in ~/.ava/.env or export before starting"
        )
    return key


class ProviderRegistrationError(ValueError):
    """A provider installation (`CatalogBuilder.install`) violated the provider-registration contract — a
    duplicate or nested prefix, a model/binding mismatch, an unpriced spawnable
    model, malformed price data.

    Distinct from an arbitrary module-body exception on purpose: the loader
    contains those (skip + loud report, fail-soft), but the prefix and
    model-id maps are flat — a collision has no precedence order to resolve
    it — so the loader lets this class propagate (fail-closed; user ruling
    2026-09-11 draws that line).
    """


@dataclass(frozen=True)
class ProviderContribution:
    """What one provider plugin declares: the dispatch binding, the chat-model rows it owns and
    their live prices. A `provider.py` returns these from `contribute()`
    (`PluginContributions.providers`); `CatalogBuilder.install` is the only thing that acts on them."""

    binding: ProviderBinding
    models: Mapping[str, ModelSpec]
    pricing: Mapping[str, PriceRates]
