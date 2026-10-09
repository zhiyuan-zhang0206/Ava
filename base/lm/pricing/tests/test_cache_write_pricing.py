"""Cache writes are input subdivisions billed at their served TTL rates."""

from datetime import UTC, datetime
from typing import Any

import pytest
from anthropic.types import Usage
from langchain_anthropic.chat_models import _create_usage_metadata
from langchain_core.messages import AIMessage

from base.lm import pricing
from base.lm.catalog import ModelCatalog
from base.lm.pricing import cost_usd, plugin_model_price, quote
from base.lm.pricing.cache_writes import cache_write_tokens
from base.lm.provider_api import PricePeriod, PriceTier
from base.lm.usage import log_usage_from_message


@pytest.fixture
def prices(model_catalog: ModelCatalog) -> pricing.PriceBook:
    return model_catalog.prices


def test_mixed_ttl_usage_from_pinned_sdk_prices_each_input_token_once(
    prices: pricing.PriceBook,
) -> None:
    usage = Usage.model_validate(
        {
            "input_tokens": 100,
            "output_tokens": 50,
            "cache_read_input_tokens": 200,
            "cache_creation_input_tokens": 700,
            "cache_creation": {"ephemeral_5m_input_tokens": 300, "ephemeral_1h_input_tokens": 400},
        }
    )
    metadata = _create_usage_metadata(usage)
    assert metadata["input_tokens"] == 1_000
    writes = cache_write_tokens(metadata.get("input_token_details") or {})
    assert writes == (300, 400)
    priced = quote(
        "claude-opus-5-5",
        1_000,
        50,
        200,
        cache_write_5m=writes[0],
        cache_write_1h=writes[1],
        prices=prices,
    )
    assert priced is not None
    # 100 ordinary input + 200 cache reads + 300 five-minute writes + 400 hour writes.
    assert priced.cost_usd == pytest.approx(
        (100 * 4 + 200 * 0.2 + 300 * 5 + 400 * 8 + 50 * 20) / 1_000_000
    )
    assert (
        cost_usd(
            "claude-opus-5-5", 1_000, 50, 200, cache_write_5m=300, cache_write_1h=400, prices=prices
        )
        == priced.cost_usd
    )


@pytest.mark.parametrize(
    "details,expected",
    [
        ({}, (0, 0)),
        ({"cache_creation": 700}, (700, 0)),
        (
            {
                "cache_creation": 700,
                "ephemeral_5m_input_tokens": 300,
                "ephemeral_1h_input_tokens": 400,
            },
            (300, 400),
        ),
        (
            {
                "cache_creation": 0,
                "ephemeral_5m_input_tokens": 300,
                "ephemeral_1h_input_tokens": 400,
            },
            (300, 400),
        ),
    ],
)
def test_legacy_and_ttl_usage_do_not_double_count(
    details: dict[str, int], expected: tuple[int, int]
) -> None:
    assert cache_write_tokens(details) == expected


@pytest.mark.parametrize(
    "details",
    [
        {"cache_creation": -1},
        {"cache_creation": 500, "ephemeral_5m_input_tokens": 300},
    ],
)
def test_cache_write_usage_rejects_invalid_counts(details: dict[str, int]) -> None:
    with pytest.raises(ValueError):
        cache_write_tokens(details)


def test_cache_write_usage_rejects_bool() -> None:
    with pytest.raises(TypeError):
        cache_write_tokens({"cache_creation": False})


@pytest.mark.parametrize("five,hour", [(-1, 0), (0, -1), (500, 400)])
def test_quote_rejects_writes_exceeding_total_input(
    prices: pricing.PriceBook, five: int, hour: int
) -> None:
    with pytest.raises(ValueError):
        quote(
            "claude-opus-5-5",
            1_000,
            50,
            200,
            cache_write_5m=five,
            cache_write_1h=hour,
            prices=prices,
        )


def test_reported_writes_without_declared_rates_remain_unpriced(prices: pricing.PriceBook) -> None:
    assert quote("gpt-6.1-sol", 100, 10, 0, cache_write_5m=100, prices=prices) is None


@pytest.mark.parametrize("rate", [-1.0, float("nan"), float("inf")])
def test_invalid_flat_write_rate_is_rejected_even_with_periods(rate: float) -> None:
    with pytest.raises(ValueError, match="finite and non-negative"):
        plugin_model_price(
            "fixture",
            cache_miss=4,
            cache_hit=0.2,
            output=20,
            cache_write_5m=rate,
            source_url="https://example.com/pricing",
            source_checked_at="2026-10-08",
            periods=(PricePeriod(None, None, (PriceTier(0, None, "4", "0.2", "20"),)),),
        )


@pytest.mark.parametrize("speed,multiplier", [("fast", 2), ("standard", 1)])
def test_usage_snapshot_prices_cache_writes_at_actual_served_speed(
    model_catalog: ModelCatalog, loguru_records: list[dict[str, Any]], speed: str, multiplier: int
) -> None:
    message = AIMessage(
        content="answer",
        response_metadata={"speed": speed},
        usage_metadata={
            "input_tokens": 1_000,
            "output_tokens": 50,
            "total_tokens": 1_050,
            "input_token_details": {
                "cache_read": 200,
                "cache_creation": 700,
                "ephemeral_5m_input_tokens": 300,
                "ephemeral_1h_input_tokens": 400,
            },
        },
    )
    result = log_usage_from_message(
        message,
        "claude-opus-5-5-fast",
        priced_at=datetime(2026, 10, 8, tzinfo=UTC),
        catalog=model_catalog,
    )
    assert result is not None
    expected = 0.00614 * multiplier
    assert result == pytest.approx((1_050, expected))
    snapshot = next(r["extra"] for r in loguru_records if r["extra"].get("event") == "llm_usage")
    assert snapshot["cache_write_5m"] == 300
    assert snapshot["cache_write_1h"] == 400
    assert snapshot["price_write_5m"] == 5 * multiplier
    assert snapshot["price_write_1h"] == 8 * multiplier
    assert snapshot["cost_usd"] == pytest.approx(expected)
