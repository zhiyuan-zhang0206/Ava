"""Explicit immutable catalog fixtures for model consumers."""

from collections.abc import Callable, Mapping
from dataclasses import replace

import pytest

from base.lm.catalog import ModelCatalog
from base.lm.plugin_providers import build_model_catalog
from base.lm.pricing import PriceBook
from base.lm.provider_api import ProviderBinding
from base.lm.registry import ModelSpec

__all__ = ["model_catalog"]

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
