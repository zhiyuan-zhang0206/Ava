"""cost_usd pricing-table regression lock.

The dashboard / inspector / metrics costs all flow through cost_usd against
MODEL_PRICING, so a wrong rate or a dropped model silently mis-bills every
view. These pin the priced models + the cache-read discount + the unknown-model
contract (None, not a fabricated $0).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from shared.lm.pricing import CostQuote, Rates, cost_usd, quote, rates_at

_M = 1_000_000


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        # in=1M (all cache-miss) + out=1M + cached=0 -> miss_rate + out_rate USD.
        ("claude-opus-4-8", 5.0 + 25.0),
        ("gemini-3.5-flash", 1.5 + 9.0),
        # 1M input selects Gemini's documented >200K tier.
        ("gemini-3.1-pro-preview", 4.0 + 18.0),
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


def test_qwen_implicit_cache_hit_is_the_registered_rate() -> None:
    """Ava never sends DashScope's explicit `cache_control` block, so every qwen
    cache hit is an IMPLICIT one — the catalog's cache_read must therefore be
    Alibaba's published implicit rate ($0.206/M for qwen3.8-max in Beijing), not
    the cheaper explicit-read rate ($0.137/M) the same page also lists. A fully
    cached 1M input bills 1M * 0.206/M."""
    assert cost_usd("qwen3.8-max", _M, 0, _M) == pytest.approx(0.206)  # pyright: ignore[reportUnknownMemberType]
    assert cost_usd("qwen3.8-max", _M, _M, 0) == pytest.approx(1.65 + 4.951)  # pyright: ignore[reportUnknownMemberType]


def test_qwen_27b_uses_its_own_published_usd_not_a_derived_rate() -> None:
    """Locks the published Beijing USD column, because the tempting shortcut is
    wrong. Alibaba prices per model rather than converting at one rate: 27b's
    CNY (3 / 0.6 / 12) at qwen3.8-max's implied ~7.2727 gives 0.4125 / 0.0825 /
    1.65, but the page publishes 0.424 / 0.085 / 1.696 — its own rate is ~7.08.
    A derived catalog would carry that ~3% error on every 27b cost row forever."""
    assert cost_usd("qwen3.8-27b", _M, _M, 0) == pytest.approx(0.424 + 1.696)  # pyright: ignore[reportUnknownMemberType]
    assert cost_usd("qwen3.8-27b", _M, 0, _M) == pytest.approx(0.085)  # pyright: ignore[reportUnknownMemberType]
    # and it is genuinely cheaper than the flagship, the reason it is registered
    cheap = cost_usd("qwen3.8-27b", _M, _M, 0)
    flagship = cost_usd("qwen3.8-max", _M, _M, 0)
    assert cheap is not None and flagship is not None
    assert cheap < flagship


def test_qwen3_8_flash_uses_the_official_qwencloud_usd() -> None:
    """Locks the official QwenCloud USD figures ($0.16 / $0.016 / $0.47), the
    only official USD published at onboarding — the Model Studio EN page
    (Beijing USD column) was not yet live on 2026-08-27, and the no-conversion
    rule forbids deriving Beijing USD from the published CNY (1 / 0.1 / 3).
    Re-check when the alibabacloud.com page lands (see the module docstring)."""
    assert cost_usd("qwen3.8-flash", _M, _M, 0) == pytest.approx(0.16 + 0.47)  # pyright: ignore[reportUnknownMemberType]
    assert cost_usd("qwen3.8-flash", _M, 0, _M) == pytest.approx(0.016)  # pyright: ignore[reportUnknownMemberType]
    # and it is genuinely cheaper than the flagship, the reason it is registered
    cheap = cost_usd("qwen3.8-flash", _M, _M, 0)
    assert cheap is not None and cheap < 1.65 + 4.951


def test_cost_usd_unknown_model_is_none() -> None:
    """A model absent from MODEL_PRICING returns None (unpriced), never a
    fabricated $0 — the caller counts it as unpriced rather than free."""
    assert cost_usd("no-such-model", 1000, 1000, 0) is None


def test_cost_usd_none_tokens_is_none() -> None:
    assert cost_usd("gpt-5.6-sol", None, None, None) is None


def test_deepseek_v4_effective_interval_boundary() -> None:
    """The new schedule starts at one exact instant: [from, until)."""
    before = datetime(2026, 8, 16, 15, 59, 59, 999999, tzinfo=UTC)
    cutover = datetime(2026, 8, 16, 16, 0, tzinfo=UTC)

    assert rates_at("deepseek-v4-pro", before, _M) == Rates(0.435, 0.003625, 0.87)
    assert rates_at("deepseek-v4-pro", cutover, _M) == Rates(0.66, 0.022, 1.98)


def test_glm_5_pricing_effective_interval_boundary() -> None:
    """GLM-5.2 preserves its prior rate before the GLM-5.3 launch cutover."""
    before = datetime(2026, 8, 13, 9, 59, 59, 999999, tzinfo=UTC)
    cutover = datetime(2026, 8, 13, 10, 0, tzinfo=UTC)

    assert rates_at("glm-5.2", before, _M) == Rates(1.10, 0.55, 3.86)
    assert rates_at("glm-5.2", cutover, _M) == Rates(1.40, 0.26, 4.40)
    assert rates_at("glm-5.3", cutover, _M) == Rates(1.40, 0.26, 4.40)


def test_glm_5_3_flash_launch_discount_boundary() -> None:
    """GLM-5.3-Flash ships at a 50% launch discount (docs.z.ai pricing page)
    ending 24:00 2026-09-09 UTC+8 = 2026-09-09T16:00:00Z; list rates follow."""
    before = datetime(2026, 9, 9, 15, 59, 59, 999999, tzinfo=UTC)
    cutover = datetime(2026, 9, 9, 16, 0, tzinfo=UTC)

    assert rates_at("glm-5.3-flash", before, _M) == Rates(0.075, 0.015, 0.25)
    assert rates_at("glm-5.3-flash", cutover, _M) == Rates(0.15, 0.03, 0.50)


@pytest.mark.parametrize(
    ("at", "expected"),
    [
        (datetime(2026, 8, 17, 0, 59, 59, 999999, tzinfo=UTC), Rates(0.66, 0.022, 1.98)),
        (datetime(2026, 8, 17, 1, 0, tzinfo=UTC), Rates(1.32, 0.044, 3.96)),
        (datetime(2026, 8, 17, 3, 59, 59, 999999, tzinfo=UTC), Rates(1.32, 0.044, 3.96)),
        (datetime(2026, 8, 17, 4, 0, tzinfo=UTC), Rates(0.66, 0.022, 1.98)),
        (datetime(2026, 8, 17, 6, 0, tzinfo=UTC), Rates(1.32, 0.044, 3.96)),
        (datetime(2026, 8, 17, 10, 0, tzinfo=UTC), Rates(0.66, 0.022, 1.98)),
    ],
)
def test_deepseek_v4_utc_peak_window_boundaries(at: datetime, expected: Rates) -> None:
    """Daily peak windows are [01:00,04:00) and [06:00,10:00) UTC."""
    assert rates_at("deepseek-v4-pro", at, _M) == expected


def test_peak_windows_normalize_an_aware_non_utc_instant() -> None:
    utc_plus_8 = timezone(timedelta(hours=8))
    at = datetime(2026, 8, 17, 9, 0, tzinfo=utc_plus_8)
    # Keep the test's offset explicit without relying on the machine timezone:
    # 09:00+08:00 is the inclusive 01:00 UTC peak boundary.
    assert at.utcoffset() == timedelta(hours=8)
    assert rates_at("deepseek-v4-flash", at, _M) == Rates(0.44, 0.014, 1.32)


def test_input_token_tier_boundary_is_inclusive() -> None:
    at = datetime(2026, 8, 18, tzinfo=UTC)
    assert rates_at("gemini-3.1-pro-preview", at, 200_000) == Rates(2.0, 0.2, 12.0)
    assert rates_at("gemini-3.1-pro-preview", at, 200_001) == Rates(4.0, 0.4, 18.0)


def test_gemini_3_7_date_only_increase_uses_earliest_global_boundary() -> None:
    """The source omits a timezone, so UTC+14 prevents underestimating cost."""
    before = datetime(2026, 12, 31, 9, 59, 59, 999999, tzinfo=UTC)
    after = datetime(2026, 12, 31, 10, 0, tzinfo=UTC)

    assert rates_at("gemini-3.7-flash", before, _M) == Rates(0.75, 0.075, 3.75)
    assert rates_at("gemini-3.7-flash", after, _M) == Rates(1.5, 0.15, 7.5)


def test_quote_returns_cost_and_the_exact_selected_rates() -> None:
    at = datetime(2026, 8, 17, 1, 0, tzinfo=UTC)
    result = quote("deepseek-v4-pro", _M, _M, _M, at=at)

    assert result is not None
    assert result == CostQuote(cost_usd=4.004, rates=Rates(1.32, 0.044, 3.96))
    assert cost_usd("deepseek-v4-pro", _M, _M, _M, at=at) == result.cost_usd


def test_rates_at_rejects_a_naive_instant() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        rates_at("deepseek-v4-pro", datetime.fromisoformat("2026-08-17T01:00:00"), _M)


@pytest.mark.parametrize(
    ("tok_in", "tok_out", "tok_cached"),
    [(-1, 0, 0), (0, -1, 0), (0, 0, -1), (10, 0, 11)],
)
def test_quote_rejects_impossible_token_usage(tok_in: int, tok_out: int, tok_cached: int) -> None:
    with pytest.raises(ValueError, match="token"):
        quote("gpt-5.6-sol", tok_in, tok_out, tok_cached)


def test_rates_at_rejects_a_negative_tier_input() -> None:
    with pytest.raises(ValueError, match="input_tokens"):
        rates_at("gemini-3.1-pro-preview", input_tokens=-1)
