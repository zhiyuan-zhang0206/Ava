"""Runtime locks for complete provider-plugin pricing semantics."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from base.lm import pricing
from base.lm.catalog import ModelCatalog
from base.lm.pricing import CostQuote, Rates, _parse_catalog, quote, rates_at

_ARCHIVE_PATH = Path(__file__).resolve().parents[4] / "base/lm/pricing/pricing_catalog_archive.json"
_HISTORICAL = datetime(2026, 8, 1, tzinfo=UTC)
_CURRENT = datetime(2026, 9, 5, tzinfo=UTC)
_PEAK_WINDOW = datetime(2026, 9, 5, 2, tzinfo=UTC)
_FUTURE = datetime(2027, 1, 5, tzinfo=UTC)


def _archive_catalog() -> dict[str, pricing.ModelPrice]:
    raw = cast(dict[str, Any], json.loads(_ARCHIVE_PATH.read_text(encoding="utf-8")))
    return _parse_catalog(raw)


def test_qa_pricing_scenarios_use_plugin_runtime_semantics(model_catalog: ModelCatalog) -> None:
    assert rates_at(
        "deepseek-v4-pro", _PEAK_WINDOW, 1_000_000, prices=model_catalog.prices
    ) == Rates(1.32, 0.044, 3.96)
    assert rates_at(
        "gemini-3.1-pro-preview", _CURRENT, 200_001, prices=model_catalog.prices
    ) == Rates(4.0, 0.4, 18.0)
    assert rates_at(
        "glm-5.3-flash", datetime(2026, 9, 15, tzinfo=UTC), 1, prices=model_catalog.prices
    ) == Rates(0.15, 0.03, 0.50)
    assert rates_at(
        "glm-5.3-flashx", datetime(2026, 9, 15, tzinfo=UTC), 1, prices=model_catalog.prices
    ) == Rates(0.37, 0.075, 1.25)
    assert rates_at("deepseek-v4-pro", _HISTORICAL, 1, prices=model_catalog.prices) == Rates(
        0.435, 0.003625, 0.87
    )


def test_all_plugin_models_match_archive_at_four_instant_classes(
    model_catalog: ModelCatalog,
) -> None:
    archive = _archive_catalog()

    assert model_catalog.prices.plugin, "no plugin prices loaded"
    for model, plugin_price in sorted(model_catalog.prices.plugin.items()):
        assert model in archive
        input_tokens = 200_001 if model == "gemini-3.1-pro-preview" else 1_000_000
        for instant in (_HISTORICAL, _CURRENT, _PEAK_WINDOW, _FUTURE):
            assert rates_at(model, instant, input_tokens, prices=model_catalog.prices) == archive[
                model
            ].rates_at(instant, input_tokens), f"{model} differs at {instant.isoformat()}"
        assert plugin_price.periods == archive[model].periods


def test_new_model_rates_and_272k_boundary(model_catalog: ModelCatalog) -> None:
    assert rates_at("claude-opus-5-5", _FUTURE, 1_000_000, prices=model_catalog.prices) == Rates(
        4.0, 0.20, 20.0, cache_write_5m=5, cache_write_1h=8
    )
    assert rates_at("claude-sonnet-5-5", _FUTURE, 1_000_000, prices=model_catalog.prices) == Rates(
        2.0, 0.20, 10.0, cache_write_5m=2.5, cache_write_1h=4
    )
    for model, tier1, tier2 in (
        ("gpt-6-sol", Rates(2.0, 0.20, 10.0), Rates(4.0, 0.40, 15.0)),
        ("gpt-6.1-sol", Rates(2.0, 0.10, 10.0), Rates(4.0, 0.20, 15.0)),
        ("gpt-6-luna", Rates(0.10, 0.01, 0.50), Rates(0.20, 0.02, 0.75)),
    ):
        assert rates_at(model, _FUTURE, 272_000, prices=model_catalog.prices) == tier1
        assert rates_at(model, _FUTURE, 272_001, prices=model_catalog.prices) == tier2


def test_future_plugin_period_is_used_by_quote_without_bot_sync(
    model_catalog: ModelCatalog,
) -> None:
    result = quote(
        "gemini-3.7-flash",
        1_000_000,
        1_000_000,
        1_000_000,
        at=datetime(2027, 1, 2, tzinfo=UTC),
        prices=model_catalog.prices,
    )

    assert result == CostQuote(cost_usd=7.65, rates=Rates(1.50, 0.15, 7.50))


def test_flat_plugin_price_remains_an_unbounded_compatibility_shortcut() -> None:
    model = "fixture-flat-price"
    price = pricing.plugin_model_price(
        model,
        cache_miss=1.0,
        cache_hit=0.1,
        output=3.0,
        source_url="https://example.com/pricing",
        source_checked_at="2026-09-05",
        vendor="fixture",
        plugin="fixture",
    )
    book = pricing.PriceBook({}, {model: price})

    assert book.rates_at(model, datetime(1900, 1, 1, 2, tzinfo=UTC), 300_000) == Rates(
        1.0, 0.1, 3.0
    )
    assert book.rates_at(model, datetime(2100, 1, 1, 2, tzinfo=UTC), 300_000) == Rates(
        1.0, 0.1, 3.0
    )


def test_haiku_5_5_whole_request_price_boundary_includes_cached_input(
    model_catalog: ModelCatalog,
) -> None:
    low = Rates(0.10, 0.01, 0.50, cache_write_5m=0.125, cache_write_1h=0.20)
    high = Rates(0.50, 0.05, 2.50, cache_write_5m=0.625, cache_write_1h=1.0)
    assert rates_at("claude-haiku-5-5", _FUTURE, 100_000, prices=model_catalog.prices) == low
    assert rates_at("claude-haiku-5-5", _FUTURE, 100_001, prices=model_catalog.prices) == high
    # Cache reads and both cache-write TTLs count toward the input threshold.
    for total, rates in ((100_000, low), (100_001, high)):
        expected = (
            (total - 90_000) * rates.cache_miss
            + 80_000 * rates.cache_hit
            + 5_000 * (rates.cache_write_5m or 0)
            + 5_000 * (rates.cache_write_1h or 0)
            + 1_000 * rates.output
        ) / 1_000_000
        assert quote(
            "claude-haiku-5-5",
            total,
            1_000,
            80_000,
            at=_FUTURE,
            cache_write_5m=5_000,
            cache_write_1h=5_000,
            prices=model_catalog.prices,
        ) == CostQuote(cost_usd=expected, rates=rates)
