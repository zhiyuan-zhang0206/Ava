"""The model catalog: every model, binding, stop vocabulary and price a process knows.

A `ModelCatalog` is an immutable value. `CatalogBuilder` is how one is made: the provider
loader (`base/lm/plugin_providers.py`) installs each enabled plugin's `ProviderContribution`
into a builder and takes `build()`, which validates the whole graph. Nothing is registered at
import and nothing is mutated after `build()`; a failed installation discards the builder, so
a retry starts from scratch. Each process composition root retains the catalog it built and passes it explicitly
to provider consumers.

The prefix map is flat: a duplicate prefix, or one that nests inside another (``foo-`` vs
``foo-bar-``), fails fast, as does a duplicate model id or stop vocabulary key — two claimants
have no precedence order to resolve them.

Decision models (``base/lm/catalog/decide``) live in parallel tables, ``decision_models`` and
``decision_providers``, installed by the same builder. They never appear in ``models``, so no
chat path (spawn validation, ``build_chat_model``, the model dropdown) can reach them. Prefix
collisions are checked across both kinds, except that one plugin may hold a chat prefix and a
decision prefix of the same vendor.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from functools import cached_property
from types import MappingProxyType

from base.lm.catalog.decide.types import DecisionModelSpec
from base.lm.catalog.provider_contract import (
    DecisionProviderContribution,
    ProviderBinding,
    ProviderContribution,
    ProviderRegistrationError,
)
from base.lm.pricing import ModelPrice, PriceBook, load_archive, plugin_model_price
from base.lm.registry import ModelSpec, validate_models, validate_spec
from base.lm.stop import StopSpec


@dataclass(frozen=True)
class ModelCatalog:
    """The registered models, provider bindings, terminal-reason vocabularies and prices."""

    models: Mapping[str, ModelSpec]
    bindings: Mapping[str, ProviderBinding]
    stops: Mapping[str, StopSpec]
    prices: PriceBook
    decision_models: Mapping[str, DecisionModelSpec] = field(
        default_factory=lambda: MappingProxyType({})
    )
    """Decision-model rows, keyed by id. Disjoint from ``models`` by construction."""
    decision_providers: Mapping[str, DecisionProviderContribution] = field(
        default_factory=lambda: MappingProxyType({})
    )
    """Decision providers keyed by their id prefix."""

    @cached_property
    def supported_models(self) -> Mapping[str, list[str]]:
        """Models offered in the frontend spawn dropdown + per-agent overlay, by provider.

        Registration order within a provider. Provider availability still depends on the
        corresponding API key being set on the agent-runner.
        """
        grouped: dict[str, list[str]] = {}
        for model_id, spec in self.models.items():
            if spec.spawnable:
                grouped.setdefault(spec.provider, []).append(model_id)
        return MappingProxyType(grouped)

    @cached_property
    def context_windows(self) -> Mapping[str, int]:
        """Max input tokens per model; a model without a known window is absent."""
        return MappingProxyType(
            {
                model_id: spec.context_window
                for model_id, spec in self.models.items()
                if spec.context_window is not None
            }
        )

    @cached_property
    def knowledge_cutoffs(self) -> Mapping[str, str]:
        """Knowledge cutoff (YYYY-MM) per model, for the system prompt; absent = no line."""
        return MappingProxyType(
            {
                model_id: spec.knowledge_cutoff
                for model_id, spec in self.models.items()
                if spec.knowledge_cutoff is not None
            }
        )

    @cached_property
    def identities(self) -> Mapping[str, str]:
        """Per-model identity note, injected before the knowledge cutoff in the system prompt."""
        return MappingProxyType(
            {
                model_id: spec.model_identity
                for model_id, spec in self.models.items()
                if spec.model_identity is not None
            }
        )

    def provider_key_of(self, model: str) -> str | None:
        """Provider key of a dispatch prefix match: the binding's explicit key or its prefix
        without the trailing dash. None for an unregistered prefix."""
        for prefix, binding in self.bindings.items():
            if model.startswith(prefix):
                return binding.provider_key or prefix.rstrip("-")
        return None

    def decision_provider_of(self, model: str) -> DecisionProviderContribution | None:
        """The decision provider owning ``model`` by prefix, or None."""
        for prefix, provider in self.decision_providers.items():
            if model.startswith(prefix):
                return provider
        return None


def _check_prefix(
    prefix: str,
    plugin: str,
    existing_prefixes: Iterable[str],
    other_kind_owners: Mapping[str, str],
) -> None:
    """Reject a malformed, duplicate or nested prefix.

    ``existing_prefixes`` are the same kind's prefixes (any owner);
    ``other_kind_owners`` maps the other kind's prefixes to their plugins, and only another
    plugin's prefix conflicts.
    """
    if not prefix or not prefix.endswith("-"):
        raise ProviderRegistrationError(
            f"provider plugin {plugin!r}: prefix {prefix!r} must end with '-' "
            "(e.g. 'foo-'); dispatch is model.startswith(prefix)"
        )
    for existing, owner in other_kind_owners.items():
        if owner != plugin and (prefix.startswith(existing) or existing.startswith(prefix)):
            raise ProviderRegistrationError(
                f"provider plugin {plugin!r}: prefix {prefix!r} collides with prefix "
                f"{existing!r} of plugin {owner!r} (chat and decision providers share one "
                "prefix namespace across plugins)"
            )
    for existing in existing_prefixes:
        if prefix == existing:
            raise ProviderRegistrationError(
                f"provider plugin {plugin!r}: prefix {prefix!r} already claimed "
                "by another plugin — the prefix map is flat and a "
                "collision is an error, not a precedence order"
            )
        if prefix.startswith(existing) or existing.startswith(prefix):
            raise ProviderRegistrationError(
                f"provider plugin {plugin!r}: prefix {prefix!r} nests inside "
                f"existing prefix {existing!r} — nested prefixes are an ordered "
                "fallback chain wearing another name; rejected"
            )


class CatalogBuilder:
    """Accumulates provider declarations; `build()` validates and freezes them."""

    def __init__(self) -> None:
        self._models: dict[str, ModelSpec] = {}
        self._bindings: dict[str, ProviderBinding] = {}
        self._stops: dict[str, StopSpec] = {}
        self._plugin_prices: dict[str, ModelPrice] = {}
        self._decision_models: dict[str, DecisionModelSpec] = {}
        self._decision_providers: dict[str, DecisionProviderContribution] = {}
        self._chat_prefix_owner: dict[str, str] = {}
        self._decision_prefix_owner: dict[str, str] = {}
        self._archive = load_archive()

    @property
    def has_bindings(self) -> bool:
        return bool(self._bindings)

    def install(self, plugin: str, contribution: ProviderContribution) -> None:
        """Install one declared provider. Models validate first, then prices, the stop
        vocabulary and the binding.

        A contract violation raises `ProviderRegistrationError` (a ValueError): installation
        is fail-fast, not best-effort, and the loader propagates this class instead of
        containing it — the flat maps cannot pick a winner between two claimants.
        """
        self._install(plugin, contribution)

    def _install(self, plugin: str, contribution: ProviderContribution) -> None:
        binding = contribution.binding
        models = contribution.models
        pricing = contribution.pricing
        _check_prefix(binding.prefix, plugin, self._bindings, self._decision_prefix_owner)
        self._validate_model_declarations(plugin, contribution)
        prices = PriceBook(self._archive, self._plugin_prices)
        for model_id, spec in models.items():
            if spec.fast_of is not None and binding.served_speed is None:
                raise ProviderRegistrationError(
                    f"Fast model {model_id!r} needs a served_speed adapter"
                )
            try:
                validate_spec(
                    model_id,
                    spec,
                    anthropic_protocol=binding.anthropic_protocol,
                    prices=prices,
                    pending_price_models=pricing.keys(),
                )
            except (ValueError, RuntimeError) as exc:
                raise ProviderRegistrationError(str(exc)) from exc
        try:
            new_prices = {
                model_id: plugin_model_price(
                    model_id,
                    cache_miss=price.cache_miss,
                    cache_hit=price.cache_hit,
                    output=price.output,
                    source_url=price.source_url,
                    source_checked_at=price.source_checked_at,
                    vendor=price.vendor,
                    periods=price.periods,
                    cache_write_5m=price.cache_write_5m,
                    cache_write_1h=price.cache_write_1h,
                    plugin=plugin,
                )
                for model_id, price in pricing.items()
            }
        except ValueError as exc:
            raise ProviderRegistrationError(str(exc)) from exc
        stop_spec = binding.stop_spec
        if stop_spec is not None and stop_spec.provider_key in self._stops:
            raise ProviderRegistrationError(
                f"provider plugin {plugin!r}: stop vocabulary key {stop_spec.provider_key!r} already "
                "registered — bind the existing client class's entry instead of "
                "re-declaring it"
            )

        self._models.update(models)
        self._plugin_prices.update(new_prices)
        if stop_spec is not None:
            self._stops[stop_spec.provider_key] = stop_spec
        self._bindings[binding.prefix] = binding
        self._chat_prefix_owner[binding.prefix] = plugin

    def install_decision(self, plugin: str, contribution: DecisionProviderContribution) -> None:
        """Install one declared decision provider; a contract violation raises
        `ProviderRegistrationError` and leaves the builder unchanged."""
        prefix = contribution.prefix
        _check_prefix(prefix, plugin, self._decision_providers, self._chat_prefix_owner)
        if set(contribution.pricing) != set(contribution.models):
            raise ProviderRegistrationError(
                f"decision plugin {plugin!r}: every decision model needs exactly one price; "
                f"models {sorted(contribution.models)!r}, priced {sorted(contribution.pricing)!r}"
            )
        for model_id in contribution.models:
            if not model_id.startswith(prefix):
                raise ProviderRegistrationError(
                    f"decision plugin {plugin!r}: model id {model_id!r} must start with "
                    f"the provider prefix {prefix!r}"
                )
            if model_id in self._decision_models:
                raise ProviderRegistrationError(
                    f"decision model id {model_id!r} is already registered by an earlier plugin"
                )
            if model_id in self._models:
                raise ProviderRegistrationError(
                    f"decision plugin {plugin!r}: model id {model_id!r} is also a chat model id — "
                    "decision ids are separate so usage pricing and chat dispatch stay unambiguous"
                )
        try:
            new_prices = {
                model_id: plugin_model_price(
                    model_id,
                    cache_miss=price.cache_miss,
                    cache_hit=price.cache_hit,
                    output=price.output,
                    source_url=price.source_url,
                    source_checked_at=price.source_checked_at,
                    vendor=price.vendor,
                    periods=price.periods,
                    cache_write_5m=price.cache_write_5m,
                    cache_write_1h=price.cache_write_1h,
                    plugin=plugin,
                )
                for model_id, price in contribution.pricing.items()
            }
        except ValueError as exc:
            raise ProviderRegistrationError(str(exc)) from exc
        clashing = new_prices.keys() & self._plugin_prices.keys()
        if clashing:
            raise ProviderRegistrationError(
                f"decision plugin {plugin!r}: prices for {sorted(clashing)!r} already declared"
            )
        self._decision_models.update(contribution.models)
        self._plugin_prices.update(new_prices)
        self._decision_providers[prefix] = contribution
        self._decision_prefix_owner[prefix] = plugin

    def _validate_model_declarations(self, plugin: str, contribution: ProviderContribution) -> None:
        """Reject declaration ownership collisions before changing the builder."""
        binding = contribution.binding
        models = contribution.models
        pricing = contribution.pricing
        provider = binding.provider_key or binding.prefix.rstrip("-")

        extra_prices = set(pricing) - set(models)
        if extra_prices:
            raise ProviderRegistrationError(
                f"provider plugin {plugin!r}: prices declared for unregistered models "
                f"{sorted(extra_prices)!r}"
            )
        for model_id, spec in models.items():
            if not model_id.startswith(binding.prefix):
                raise ProviderRegistrationError(
                    f"provider plugin {plugin!r}: model id {model_id!r} must start with "
                    f"the binding prefix {binding.prefix!r} so factory dispatch can reach it"
                )
            if spec.provider != provider:
                raise ProviderRegistrationError(
                    f"provider plugin {plugin!r}: model {model_id!r} declares "
                    f"provider {spec.provider!r} but the binding prefix {binding.prefix!r} "
                    f"implies {provider!r} — fix the ModelSpec.provider"
                )
            if model_id in self._models:
                raise ProviderRegistrationError(
                    f"model id {model_id!r} is already registered by an earlier plugin — "
                    "model ids are flat and a duplicate is an error"
                )
            if model_id in self._decision_models:
                raise ProviderRegistrationError(
                    f"model id {model_id!r} is already a decision model id — "
                    "chat and decision ids are disjoint"
                )

    def build(self) -> ModelCatalog:
        """Validate the complete model graph and freeze it."""
        prices = PriceBook(self._archive, self._plugin_prices)
        anthropic_protocol_by_model = {
            model_id: binding.anthropic_protocol
            for prefix, binding in self._bindings.items()
            for model_id in self._models
            if model_id.startswith(prefix)
        }
        validate_models(
            self._models, prices=prices, anthropic_protocol_by_model=anthropic_protocol_by_model
        )
        return ModelCatalog(
            models=MappingProxyType(dict(self._models)),
            bindings=MappingProxyType(dict(self._bindings)),
            stops=MappingProxyType(dict(self._stops)),
            prices=prices,
            decision_models=MappingProxyType(dict(self._decision_models)),
            decision_providers=MappingProxyType(dict(self._decision_providers)),
        )
