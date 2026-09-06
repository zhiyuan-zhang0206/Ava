"""Runtime locks for complete provider-plugin pricing semantics."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from shared.lm import pricing
from shared.lm._plugin_providers import ensure_provider_plugins_loaded
from shared.lm.pricing import CostQuote, Rates, _parse_catalog, quote, rates_at

_ARCHIVE_PATH = Path(__file__).resolve().parents[2] / "shared/lm/pricing_catalog_archive.json"
_HISTORICAL = datetime(2026, 8, 1, tzinfo=UTC)
_CURRENT = datetime(2026, 9, 5, tzinfo=UTC)
_PEAK_WINDOW = datetime(2026, 9, 5, 2, tzinfo=UTC)
_FUTURE = datetime(2027, 1, 5, tzinfo=UTC)


def _archive_catalog() -> dict[str, pricing._ModelPrice]:
    raw = cast(dict[str, Any], json.loads(_ARCHIVE_PATH.read_text(encoding="utf-8")))
    return _parse_catalog(raw)


def setup_module() -> None:
    ensure_provider_plugins_loaded()


def test_qa_pricing_scenarios_use_plugin_runtime_semantics() -> None:
    assert rates_at("deepseek-v4-pro", _PEAK_WINDOW, 1_000_000) == Rates(1.32, 0.044, 3.96)
    assert rates_at("gemini-3.1-pro-preview", _CURRENT, 200_001) == Rates(4.0, 0.4, 18.0)
    assert rates_at("glm-5.3-flash", datetime(2026, 9, 15, tzinfo=UTC), 1) == Rates(
        0.15, 0.03, 0.50
    )
    assert rates_at("deepseek-v4-pro", _HISTORICAL, 1) == Rates(0.435, 0.003625, 0.87)


def test_all_plugin_models_match_archive_at_four_instant_classes() -> None:
    archive = _archive_catalog()

    assert len(pricing._PLUGIN_PRICES) == 34
    for model, plugin_price in sorted(pricing._PLUGIN_PRICES.items()):
        assert model in archive
        input_tokens = 200_001 if model == "gemini-3.1-pro-preview" else 1_000_000
        for instant in (_HISTORICAL, _CURRENT, _PEAK_WINDOW, _FUTURE):
            assert rates_at(model, instant, input_tokens) == archive[model].rates_at(
                instant, input_tokens
            ), f"{model} differs at {instant.isoformat()}"
        assert plugin_price.periods == archive[model].periods


def test_future_plugin_period_is_used_by_quote_without_bot_sync() -> None:
    result = quote(
        "gemini-3.7-flash",
        1_000_000,
        1_000_000,
        1_000_000,
        at=datetime(2027, 1, 2, tzinfo=UTC),
    )

    assert result == CostQuote(cost_usd=7.65, rates=Rates(1.50, 0.15, 7.50))


def test_flat_plugin_price_remains_an_unbounded_compatibility_shortcut() -> None:
    model = "fixture-flat-price"
    try:
        pricing.register_plugin_price(
            model,
            cache_miss=1.0,
            cache_hit=0.1,
            output=3.0,
            source_url="https://example.com/pricing",
            source_checked_at="2026-09-05",
            vendor="fixture",
            plugin="fixture",
        )

        assert rates_at(model, datetime(1900, 1, 1, 2, tzinfo=UTC), 300_000) == Rates(1.0, 0.1, 3.0)
        assert rates_at(model, datetime(2100, 1, 1, 2, tzinfo=UTC), 300_000) == Rates(1.0, 0.1, 3.0)
    finally:
        pricing._PLUGIN_PRICES.pop(model, None)
