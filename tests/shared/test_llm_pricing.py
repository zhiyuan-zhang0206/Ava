"""cost_usd pricing-table regression lock.

The dashboard / inspector / metrics costs all flow through cost_usd against
MODEL_PRICING, so a wrong rate or a dropped model silently mis-bills every
view. These pin the priced models + the cache-read discount + the unknown-model
contract (None, not a fabricated $0).
"""

from __future__ import annotations

import pytest

from shared.lm.pricing import cost_usd

_M = 1_000_000


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        # in=1M (all cache-miss) + out=1M + cached=0 -> miss_rate + out_rate USD.
        ("claude-opus-4-8", 5.0 + 25.0),
        ("gemini-3.5-flash", 1.5 + 9.0),
        ("gemini-3.1-pro-preview", 2.0 + 12.0),
        ("gpt-5.6-sol", 5.0 + 30.0),
        ("gpt-5.6-terra", 2.0 + 12.0),
        ("gpt-5.6-luna", 0.20 + 1.20),
        ("mimo-v2.5-pro", 0.435 + 0.87),
        ("mimo-v2.5-pro-ultraspeed", 1.305 + 2.61),
    ],
)
def test_cost_usd_priced_models(model: str, expected: float) -> None:
    assert cost_usd(model, _M, _M, 0) == pytest.approx(expected)  # pyright: ignore[reportUnknownMemberType]


def test_cost_usd_cache_read_discount() -> None:
    """A fully-cached input bills at the cache-read rate, not the miss rate:
    gpt-5.6 in=1M all cached, out=0 -> 1M * 0.50/M = $0.50."""
    assert cost_usd("gpt-5.6-sol", _M, 0, _M) == pytest.approx(0.5)  # pyright: ignore[reportUnknownMemberType]
    # mimo-v2.5-pro: ~120x cheaper cache hit (0.0036/M) vs miss (0.435/M).
    assert cost_usd("mimo-v2.5-pro", _M, 0, _M) == pytest.approx(0.0036)  # pyright: ignore[reportUnknownMemberType]


def test_cost_usd_unknown_model_is_none() -> None:
    """A model absent from MODEL_PRICING returns None (unpriced), never a
    fabricated $0 — the caller counts it as unpriced rather than free."""
    assert cost_usd("no-such-model", 1000, 1000, 0) is None


def test_cost_usd_none_tokens_is_none() -> None:
    assert cost_usd("gpt-5.6-sol", None, None, None) is None
