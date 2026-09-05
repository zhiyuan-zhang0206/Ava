"""Provider plugin contract — what a plugin's ``provider.py`` registers against.

A provider plugin makes one more vendor's models *nameable*. It never decides
which model an agent runs on — no routing, no fallback, no per-turn hook
(``shared/lm/model-providers-as-plugins.md``,
``decisions/2026-07-29-no-runtime-model-routing.md``). Registration happens
once per process, before the first build / spawn validation / model list
(``shared/lm/_plugin_providers.py`` loads every enabled plugin's
``provider.py``); the prefix map is flat — a duplicate prefix, or one that
nests inside another (``foo-`` vs ``foo-bar-``), fails fast at registration.
Core registers no providers; enabled plugins are the sole source of bindings,
chat-model rows, provider vocabularies, media fallbacks, keys, and live prices.

A ``provider.py`` module may import ``shared`` and LangChain packages only —
never ``ava`` / ``agent`` (the gateway, the labeler daemon, and the eval
harness load it, and none of those processes has an agent runtime). Plugin
discovery is keyed on ``plugin.py``, so a provider plugin ships one even when
it contributes nothing agent-side (an empty stub satisfies discovery and the
enable switch).

``ProviderBinding.key_env`` declares the key's `.env` delivery channel: the
gateway reads the file at spawn validation, bootstrap relays enabled bindings'
present keys to split runners, and the single-box agent child allowlist forwards
only those declared keys. The install seed allowlist uses the same declaration;
an unmodeled key (or an already seedable Settings alias) may seed, but unrelated
modeled settings, cluster identity/data-plane aliases, and the runner database
password cannot. Plugin config images do not carry provider secrets.

Builder contract (plain Python, documented rather than schema'd — see
``decisions/2026-07-19-plugin-core-boundary-wrapper-extension.md``):

- ``build(ctx)`` returns a ``BaseChatModel`` with no tools bound (the caller
  binds them). It is a pure function of ``ctx`` — no caller, agent, error
  history, or budget is visible.
- Missing API key raises ``RuntimeError`` immediately (``require_key``) — a
  clear build-time error, not a server 401 mid-turn.
- ``ctx.resolved_effort`` keeps the provider's established wire semantics.
  Builders with a constrained vocabulary clamp with
  ``shared.lm._effort._clamp_effort``; the GPT builder preserves the resolved
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
from typing import NamedTuple

from langchain_core.language_models.chat_models import BaseChatModel

from shared.lm._providers import ThinkingConfig
from shared.lm.pricing import register_plugin_price
from shared.lm.registry import ModelSpec, register_models
from shared.lm.stop import StopSpec, register_stop_spec

# Increment for breaking contract changes; additive optional fields stay on the
# current version. A plugin written against an older shape keeps working —
# consumers of a new field must degrade when it is absent. (Mirrors the
# plugin-spec-v2 ``engines.ava`` host-compatibility idea.)
PROVIDER_API_VERSION = 2


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
    ``shared/lm/pricing_catalog_archive.json`` so runtime and archive selection
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


class _ProviderRegistry:
    """Plugin-side registration state; consulted by ``shared/lm/factory.py``.

    Core registers no bindings. The reserved-prefix seam remains for contract
    compatibility, but the production reserved set is empty.
    """

    def __init__(self) -> None:
        self.bindings: dict[str, ProviderBinding] = {}
        self._reserved_prefixes: set[str] = set()
        self._invalidators: list[Callable[[], None]] = []

    def reserve_core_prefixes(self, prefixes: set[str]) -> None:
        self._reserved_prefixes = prefixes

    def register_invalidator(self, fn: Callable[[], None]) -> None:
        """Register a callback run after every successful plugin registration.

        Consumers that cache a view derived from the registration state (e.g.
        ``shared/lm/_concurrency.known_provider_keys``) hook their cache clear
        here so a plugin provider becomes visible without a second mechanism.
        """
        self._invalidators.append(fn)

    def _invalidate(self) -> None:
        for fn in self._invalidators:
            fn()

    def ensure_available(self, binding: ProviderBinding, *, plugin: str) -> None:
        """Reject a binding collision before its companion data mutates."""
        _check_prefix(binding.prefix, plugin, self.bindings, self._reserved_prefixes)

    def add(self, binding: ProviderBinding, *, plugin: str) -> None:
        self.ensure_available(binding, plugin=plugin)
        self.bindings[binding.prefix] = binding
        self._invalidate()


REGISTRY = _ProviderRegistry()

_CURRENT_PLUGIN: str | None = None


def _check_prefix(
    prefix: str,
    plugin: str,
    bindings: dict[str, ProviderBinding],
    reserved: set[str],
) -> None:
    if not prefix or not prefix.endswith("-"):
        raise ValueError(
            f"provider plugin {plugin!r}: prefix {prefix!r} must end with '-' "
            "(e.g. 'foo-'); dispatch is model.startswith(prefix)"
        )
    for existing in [*bindings, *reserved]:
        if prefix == existing:
            raise ValueError(
                f"provider plugin {plugin!r}: prefix {prefix!r} already claimed "
                "(by core or another plugin) — the prefix map is flat and a "
                "collision is an error, not a precedence order"
            )
        if prefix.startswith(existing) or existing.startswith(prefix):
            raise ValueError(
                f"provider plugin {plugin!r}: prefix {prefix!r} nests inside "
                f"existing prefix {existing!r} — nested prefixes are an ordered "
                "fallback chain wearing another name; rejected"
            )


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


def register(
    binding: ProviderBinding,
    *,
    models: Mapping[str, ModelSpec],
    pricing: Mapping[str, PriceRates],
) -> None:
    """The one entry point a provider.py calls. Order matters: models validate
    before prices mutate runtime state, then the stop vocabulary and binding
    land. Any failure propagates out of the loader and fails the process —
    registration is fail-fast, not best-effort.
    """
    plugin = _CURRENT_PLUGIN or "<unknown>"
    REGISTRY.ensure_available(binding, plugin=plugin)
    provider = binding.provider_key or binding.prefix.rstrip("-")

    extra_prices = set(pricing) - set(models)
    if extra_prices:
        raise ValueError(
            f"provider plugin {plugin!r}: prices declared for unregistered models "
            f"{sorted(extra_prices)!r}"
        )
    for model_id, spec in models.items():
        if not model_id.startswith(binding.prefix):
            raise ValueError(
                f"provider plugin {plugin!r}: model id {model_id!r} must start with "
                f"the binding prefix {binding.prefix!r} so factory dispatch can reach it"
            )
        if spec.provider != provider:
            raise ValueError(
                f"provider plugin {plugin!r}: model {model_id!r} declares "
                f"provider {spec.provider!r} but the binding prefix {binding.prefix!r} "
                f"implies {provider!r} — fix the ModelSpec.provider"
            )

    register_models(
        provider,
        models,
        anthropic_protocol=binding.anthropic_protocol,
        pending_price_models=pricing.keys(),
    )

    for model_id, price in pricing.items():
        register_plugin_price(
            model_id,
            cache_miss=price.cache_miss,
            cache_hit=price.cache_hit,
            output=price.output,
            source_url=price.source_url,
            source_checked_at=price.source_checked_at,
            vendor=price.vendor,
            periods=price.periods,
            plugin=plugin,
        )

    if binding.stop_spec is not None:
        register_stop_spec(binding.stop_spec, plugin=plugin)

    REGISTRY.add(binding, plugin=plugin)
