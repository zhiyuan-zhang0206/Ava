"""Shared helpers of the decision-model tests: fake bindings and catalog construction."""

from __future__ import annotations

from collections.abc import Callable, Mapping

from base.lm.catalog import CatalogBuilder, ModelCatalog
from base.lm.provider_api import (
    Decision,
    DecisionBinding,
    DecisionBuildContext,
    DecisionModelSpec,
    DecisionProviderContribution,
    DecisionResult,
    PriceRates,
    Question,
)

MODEL = "fake-1"


class FakeBinding:
    """Returns the supplied decisions and billed tokens without any network."""

    def __init__(self, decisions: Mapping[str, Decision], tokens: int = 100) -> None:
        self._decisions = decisions
        self._tokens = tokens
        self.calls: list[tuple[str, Mapping[str, Question]]] = []

    async def decide(self, state: str, questions: Mapping[str, Question]) -> DecisionResult:
        self.calls.append((state, questions))
        return DecisionResult(self._decisions, self._tokens)


def price(rate: float = 1.0) -> PriceRates:
    return PriceRates(
        cache_miss=rate,
        cache_hit=rate,
        output=0.0,
        source_url="https://example.com/pricing",
        source_checked_at="2026-10-11",
        vendor="fakevendor",
    )


def provider(
    binding: DecisionBinding,
    *,
    prefix: str = "fake-",
    models: Mapping[str, DecisionModelSpec] | None = None,
    build: Callable[[DecisionBuildContext], DecisionBinding] | None = None,
) -> DecisionProviderContribution:
    rows = models or {MODEL: DecisionModelSpec(context_window=1_000)}
    return DecisionProviderContribution(
        prefix=prefix,
        display_name="Fake",
        key_env="FAKE_API_KEY",
        build=build or (lambda _ctx: binding),
        models=rows,
        pricing={model_id: price() for model_id in rows},
    )


def catalog_with(binding: DecisionBinding, **kwargs: object) -> ModelCatalog:
    builder = CatalogBuilder()
    builder.install_decision("fake_plugin", provider(binding, **kwargs))  # pyright: ignore[reportArgumentType]
    return builder.build()
