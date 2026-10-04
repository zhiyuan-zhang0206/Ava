"""Fixtures that change the process's model catalog for one test: `add_models`, `add_bindings`, `set_prices`.

The catalog is an immutable value (`base/lm/catalog.py`), so a test that needs a model or a
provider binding the installed plugins do not declare, or one with a different fact, lends the
process a catalog with those rows (`base.lm.plugin_providers.use_catalog`) instead of writing into
a table. The rows are taken as given, unvalidated, and the process's own catalog comes back when
the test ends.

Opt-in, never autouse:

    def test_withdrawn_model(add_models):
        base = model_catalog().models["deepseek-flash"]
        add_models({"deepseek-retired": replace(base, spawnable=False, unavailable_fallback="deepseek-flash")})
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import ExitStack
from dataclasses import replace

import pytest

from base.lm.pricing import PriceBook
from base.lm.provider_api import ProviderBinding
from base.lm.registry import ModelSpec

AddModels = Callable[[Mapping[str, ModelSpec]], None]
"""The type of the `add_models` fixture's value."""

AddBindings = Callable[[Mapping[str, ProviderBinding]], None]
"""The type of the `add_bindings` fixture's value."""

SetPrices = Callable[[PriceBook], None]
"""The type of the `set_prices` fixture's value."""


@pytest.fixture
def add_models() -> Iterator[AddModels]:
    """Return a function that adds (or replaces) model rows until the test ends."""
    from base.lm.plugin_providers import model_catalog, use_catalog

    with ExitStack() as stack:

        def add(models: Mapping[str, ModelSpec]) -> None:
            catalog = model_catalog()
            stack.enter_context(use_catalog(replace(catalog, models={**catalog.models, **models})))

        yield add


@pytest.fixture
def add_bindings() -> Iterator[AddBindings]:
    """Return a function that adds (or replaces) provider bindings, by dispatch prefix, until the
    test ends."""
    from base.lm.plugin_providers import model_catalog, use_catalog

    with ExitStack() as stack:

        def add(bindings: Mapping[str, ProviderBinding]) -> None:
            catalog = model_catalog()
            stack.enter_context(
                use_catalog(replace(catalog, bindings={**catalog.bindings, **bindings}))
            )

        yield add


@pytest.fixture
def set_prices() -> Iterator[SetPrices]:
    """Return a function that replaces the catalog's price book until the test ends."""
    from base.lm.plugin_providers import model_catalog, use_catalog

    with ExitStack() as stack:

        def put(prices: PriceBook) -> None:
            stack.enter_context(use_catalog(replace(model_catalog(), prices=prices)))

        yield put
