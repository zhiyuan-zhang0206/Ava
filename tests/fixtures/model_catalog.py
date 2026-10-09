"""Explicit immutable catalog fixtures for model consumers."""

from collections.abc import Callable, Mapping
from dataclasses import replace
from pathlib import Path

import pytest

from ava.sdk_surface.install import Installation
from base.agents.messages.delivery_outbox import DeliverySenderConfig
from base.config import Settings, settings
from base.config.service_read import ConfigAuthority
from base.lm.catalog import ModelCatalog
from base.lm.plugin_providers import build_model_catalog
from base.lm.pricing import PriceBook
from base.lm.provider_api import ProviderBinding
from base.lm.registry import ModelSpec
from base.packages.plugins.extensions import EMPTY

AddModels = Callable[[ModelCatalog, Mapping[str, ModelSpec]], ModelCatalog]
AddBindings = Callable[[ModelCatalog, Mapping[str, ProviderBinding]], ModelCatalog]
SetPrices = Callable[[ModelCatalog, PriceBook], ModelCatalog]


@pytest.fixture
def model_catalog() -> ModelCatalog:
    """A catalog the test owns and passes to its consumers."""
    return build_model_catalog()


@pytest.fixture
def add_models() -> AddModels:
    """Return a catalog containing the supplied model rows."""

    def add(catalog: ModelCatalog, models: Mapping[str, ModelSpec]) -> ModelCatalog:
        return replace(catalog, models={**catalog.models, **models})

    return add


@pytest.fixture
def add_bindings() -> AddBindings:
    """Return a catalog containing the supplied bindings."""

    def add(catalog: ModelCatalog, bindings: Mapping[str, ProviderBinding]) -> ModelCatalog:
        return replace(catalog, bindings={**catalog.bindings, **bindings})

    return add


@pytest.fixture
def set_prices() -> SetPrices:
    """Return a catalog using the supplied price book."""
    return lambda catalog, prices: replace(catalog, prices=prices)


@pytest.fixture
def config_authority(unit_home: Path) -> ConfigAuthority:
    """The test's explicit boot models and unit file, with no installed holder."""
    complete = settings if settings.profile is None else Settings(profile=None)
    return ConfigAuthority(settings, complete, unit_home / ".env")


@pytest.fixture
def model_installation(
    model_catalog: ModelCatalog, config_authority: ConfigAuthority
) -> Installation:
    """Return an explicit SDK owner; the caller chooses when to install it."""
    return Installation(
        registry=EMPTY,
        expansions=(),
        wrap_layers={},
        skill_providers=(),
        metered=(),
        disabled=frozenset(),
        faces=False,
        undo=(),
        catalog=model_catalog,
        authority=config_authority,
        delivery_sender=DeliverySenderConfig(config_authority),
    )
