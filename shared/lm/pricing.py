"""Versioned LLM pricing catalog + deterministic quote selection.

``pricing_catalog.json`` is the reviewed source of truth. Its effective
intervals, input-token tiers, and recurring UTC windows select one exact rate
triple for a call; ``quote`` returns that triple with its cost atomically.
``cost_usd`` remains the compatibility surface for existing readers.

## Cache hit vs miss must be computed separately

Cache-hit input can be 30-120x cheaper than cache-miss input; in
multi-turn re-send same prefix the actual cache hit rate can be ~98%.
Using a 2-tuple (in, out) computes the entire `in` at cache-miss rate
and overestimates total cost by 30-50x — history: the same bug in
a batch run once reported $56.38 for a 30-case
SWE-bench, actual was $0.8; fix in PR #322 / commit 5c85b74. So the
pricing fact is a 3-tuple `(cache_miss, cache_hit, out)` USD/M.

Every model entry carries its own official source URL and verification date.
Providers with machine-readable pricing can be reconciled by adapters; providers
without one remain reviewed catalog updates. Runtime never scrapes pricing pages.

## DeepSeek reasoning tokens

DeepSeek's reasoning tokens count into output — billed at the merged
output rate; no separation between reasoning vs answer
(usage_metadata reports separately but the underlying rate is the
same).

## Qwen: flat tiers only, and never derive a USD rate

A tier boundary here is an exact token count and the catalog validates gapless
coverage, but Alibaba's docs express Qwen boundaries only as `Input<=256k` /
`256k<Input<=1m` — the token value of "256K" is published nowhere. Guessing
262,144 against 256,000 misprices ~3x in the band between them, so a
length-tiered Qwen must not be registered off the docs website alone. Both
registered models sidestep this: an account's own `GET /api/v1/models` reports
`"range_name": "Default"` for each, i.e. a single flat tier with no boundary to
guess. That endpoint, not the docs prose, is the authority on tiering.

**Do not convert CNY prices.** Alibaba publishes USD per model rather than
converting at one rate, and the implied rates diverge — qwen3.8-max's USD works
out to ~7.27 CNY/USD while qwen3.8-27b's is ~7.08. Deriving 27b from its CNY
figures at max's rate produced 0.4125/1.65/0.0825 against the published
0.424/1.696/0.085, a ~3% error baked permanently into every cost row. Read the
model page's own USD column.

Rates are also region-specific, and the endpoint is configurable
(`AVA_DASHSCOPE_BASE_URL`): these entries are Beijing, and Singapore prices the
same models differently (qwen3.8-max $2.00/$0.25/$6.00 against Beijing's
$1.65/$0.206/$4.951). Repointing the base URL at another region means
re-checking this catalog, not just the URL.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from pathlib import Path
from typing import Any, cast

from langchain_core.messages import AIMessage, AnyMessage


@dataclass(frozen=True)
class Rates:
    """Cache-miss input, cache-hit input, and output USD per 1M tokens."""

    cache_miss: float
    cache_hit: float
    output: float

    def as_tuple(self) -> tuple[float, float, float]:
        """Compatibility shape used by usage-event price snapshots."""
        return (self.cache_miss, self.cache_hit, self.output)


@dataclass(frozen=True)
class CostQuote:
    """One call's cost and the exact rates selected for it."""

    cost_usd: float
    rates: Rates


@dataclass(frozen=True)
class _UtcDailyOverride:
    start: time
    end: time
    rates: Rates

    def contains(self, instant: datetime) -> bool:
        current = instant.astimezone(UTC).time().replace(tzinfo=None)
        if self.start < self.end:
            return self.start <= current < self.end
        return current >= self.start or current < self.end


@dataclass(frozen=True)
class _TokenTier:
    input_tokens_min: int
    input_tokens_max: int | None
    rates: Rates
    utc_daily_overrides: tuple[_UtcDailyOverride, ...]

    def contains(self, input_tokens: int | None) -> bool:
        # A rate lookup without usage (the model picker / compatibility map)
        # intentionally publishes the base tier.
        if input_tokens is None:
            return self.input_tokens_min == 0
        return input_tokens >= self.input_tokens_min and (
            self.input_tokens_max is None or input_tokens <= self.input_tokens_max
        )

    def rates_at(self, instant: datetime) -> Rates:
        for override in self.utc_daily_overrides:
            if override.contains(instant):
                return override.rates
        return self.rates


@dataclass(frozen=True)
class _EffectivePeriod:
    effective_from: datetime | None
    effective_until: datetime | None
    tiers: tuple[_TokenTier, ...]

    def contains(self, instant: datetime) -> bool:
        return (self.effective_from is None or instant >= self.effective_from) and (
            self.effective_until is None or instant < self.effective_until
        )


@dataclass(frozen=True)
class _ModelPrice:
    source_url: str
    source_checked_at: date
    effective_time_note: str | None
    periods: tuple[_EffectivePeriod, ...]


def _instant(value: str | None, *, field: str) -> datetime | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.utcoffset() is None:
        raise RuntimeError(f"pricing catalog {field} must carry a UTC offset: {value!r}")
    return parsed.astimezone(UTC)


def _clock(value: str) -> time:
    parsed = time.fromisoformat(value)
    if parsed.tzinfo is not None:
        raise RuntimeError(f"pricing catalog UTC daily clock must be offset-free: {value!r}")
    return parsed


def _rates(raw: dict[str, Any]) -> Rates:
    values = Rates(float(raw["input"]), float(raw["cache_read"]), float(raw["output"]))
    if any(not math.isfinite(value) or value < 0 for value in values.as_tuple()):
        raise RuntimeError(f"pricing catalog rates must be finite and non-negative: {raw!r}")
    return values


_DAY_MICROSECONDS = 24 * 60 * 60 * 1_000_000


def _clock_microseconds(value: time) -> int:
    return ((value.hour * 60 + value.minute) * 60 + value.second) * 1_000_000 + value.microsecond


def _window_segments(window: _UtcDailyOverride) -> tuple[tuple[int, int], ...]:
    if window.start == window.end:
        raise RuntimeError(
            "pricing catalog UTC daily window cannot cover zero or twenty-four hours"
        )
    start = _clock_microseconds(window.start)
    end = _clock_microseconds(window.end)
    if start < end:
        return ((start, end),)
    return ((0, end), (start, _DAY_MICROSECONDS))


def _validate_windows(model: str, windows: tuple[_UtcDailyOverride, ...]) -> None:
    segments: list[tuple[int, int]] = []
    for window in windows:
        for start, end in _window_segments(window):
            if any(start < seen_end and seen_start < end for seen_start, seen_end in segments):
                raise RuntimeError(f"pricing catalog has overlapping UTC windows for {model!r}")
            segments.append((start, end))


def _parse_tiers(model: str, raw_tiers: list[dict[str, Any]]) -> tuple[_TokenTier, ...]:
    tiers: list[_TokenTier] = []
    expected_min = 0
    for raw in raw_tiers:
        lower = int(raw["input_tokens_min"])
        upper_raw = raw["input_tokens_max"]
        upper = None if upper_raw is None else int(upper_raw)
        if lower != expected_min or (upper is not None and upper < lower):
            raise RuntimeError(f"pricing catalog has a gap or overlap in tiers for {model!r}")
        windows = tuple(
            _UtcDailyOverride(
                start=_clock(item["start"]),
                end=_clock(item["end"]),
                rates=_rates(item["rates"]),
            )
            for item in cast(list[dict[str, Any]], raw["utc_daily_overrides"])
        )
        _validate_windows(model, windows)
        tiers.append(
            _TokenTier(
                input_tokens_min=lower,
                input_tokens_max=upper,
                rates=_rates(raw["rates"]),
                utc_daily_overrides=windows,
            )
        )
        expected_min = -1 if upper is None else upper + 1
    if not tiers or expected_min != -1:
        raise RuntimeError(f"pricing catalog tiers for {model!r} must cover all input sizes")
    return tuple(tiers)


def _parse_periods(model: str, raw_periods: list[dict[str, Any]]) -> tuple[_EffectivePeriod, ...]:
    periods: list[_EffectivePeriod] = []
    previous_until: datetime | None = None
    for index, raw in enumerate(raw_periods):
        start = _instant(raw["effective_from"], field="effective_from")
        end = _instant(raw["effective_until"], field="effective_until")
        if end is not None and start is not None and end <= start:
            raise RuntimeError(f"pricing catalog has an empty effective period for {model!r}")
        if index == 0 and start is not None:
            raise RuntimeError(f"pricing catalog must cover all history for {model!r}")
        if index > 0 and start != previous_until:
            raise RuntimeError(f"pricing catalog has a gap or overlap in periods for {model!r}")
        periods.append(
            _EffectivePeriod(
                effective_from=start,
                effective_until=end,
                tiers=_parse_tiers(model, cast(list[dict[str, Any]], raw["tiers"])),
            )
        )
        previous_until = end
    if not periods:
        raise RuntimeError(f"pricing catalog has no effective periods for {model!r}")
    if periods[-1].effective_until is not None:
        raise RuntimeError(f"pricing catalog must cover the present for {model!r}")
    return tuple(periods)


def _load_catalog() -> dict[str, _ModelPrice]:
    path = Path(__file__).with_name("pricing_catalog.json")
    raw = cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))
    if raw["schema_version"] != 1 or raw["currency"] != "USD" or raw["unit_tokens"] != 1_000_000:
        raise RuntimeError("unsupported pricing catalog schema, currency, or token unit")
    if not isinstance(raw["catalog_version"], str) or not raw["catalog_version"]:
        raise RuntimeError("pricing catalog version must be a non-empty string")
    catalog: dict[str, _ModelPrice] = {}
    for model, entry_raw in cast(dict[str, dict[str, Any]], raw["models"]).items():
        source_url = entry_raw["source_url"]
        if not isinstance(source_url, str) or not source_url.startswith("https://"):
            raise RuntimeError(f"pricing catalog source_url must be HTTPS for {model!r}")
        effective_time_note = entry_raw.get("effective_time_note")
        if effective_time_note is not None and (
            not isinstance(effective_time_note, str) or not effective_time_note
        ):
            raise RuntimeError(
                f"pricing catalog effective_time_note must be non-empty text for {model!r}"
            )
        catalog[model] = _ModelPrice(
            source_url=source_url,
            source_checked_at=date.fromisoformat(entry_raw["source_checked_at"]),
            effective_time_note=effective_time_note,
            periods=_parse_periods(model, cast(list[dict[str, Any]], entry_raw["periods"])),
        )
    return catalog


_CATALOG = _load_catalog()


@dataclass(frozen=True)
class _PluginPrice:
    """A plugin-declared price: flat Rates + provenance (no periods/tiers).

    Plugin prices live in plugin code — the plugin is the reviewed object;
    its freshness signal is ``source_checked_at``. The core catalog keeps its
    own stricter discipline (effective periods, tiers, windows). Runtime never
    scrapes either source.
    """

    rates: Rates
    source_url: str
    source_checked_at: str


_PLUGIN_PRICES: dict[str, _PluginPrice] = {}


def register_plugin_price(
    model: str,
    *,
    cache_miss: float,
    cache_hit: float,
    output: float,
    source_url: str,
    source_checked_at: str,
    plugin: str = "<unknown>",
) -> None:
    """Register a plugin provider's per-model price (shared/lm/provider_api.py).

    A duplicate model id is an error, whatever the source: the catalog owns
    core models, plugin prices own plugin models, and a collision is a typo or
    a rebinding — never a precedence order.
    """
    if model in _CATALOG:
        raise ValueError(
            f"provider plugin {plugin!r}: model {model!r} is priced by the core "
            "catalog — plugin pricing cannot override it"
        )
    if model in _PLUGIN_PRICES:
        raise ValueError(
            f"provider plugin {plugin!r}: model {model!r} already has a plugin "
            "price registered — duplicate registration"
        )
    if not source_url.startswith("https://"):
        raise ValueError(
            f"provider plugin {plugin!r}: source_url must be HTTPS, got {source_url!r}"
        )
    try:
        checked_at = date.fromisoformat(source_checked_at)
    except ValueError as e:
        raise ValueError(
            f"provider plugin {plugin!r}: source_checked_at must be YYYY-MM-DD, "
            f"got {source_checked_at!r}"
        ) from e
    if checked_at.isoformat() != source_checked_at:
        raise ValueError(
            f"provider plugin {plugin!r}: source_checked_at must be YYYY-MM-DD, "
            f"got {source_checked_at!r}"
        )
    for name, value in (("cache_miss", cache_miss), ("cache_hit", cache_hit), ("output", output)):
        if not math.isfinite(value) or value < 0:
            raise ValueError(
                f"provider plugin {plugin!r}: {model!r} price {name} must be finite and non-negative"
            )
    _PLUGIN_PRICES[model] = _PluginPrice(
        rates=Rates(cache_miss, cache_hit, output),
        source_url=source_url,
        source_checked_at=source_checked_at,
    )


def plugin_price_provenance(model: str) -> tuple[str, str] | None:
    """(source_url, source_checked_at) of a plugin-declared price, or None.

    The observability hook for price drift: a cost event can name which source
    its rates came from, so a plugin price stale for months is visible instead
    of silently wrong.
    """
    entry = _PLUGIN_PRICES.get(model)
    if entry is None:
        return None
    return (entry.source_url, entry.source_checked_at)


# Retired models — their FINAL published rate, frozen at retirement (add-only
# ledger). A model leaves MODELS when it is replaced (fail-fast on residual
# config_overlay references), but historical llm_usage rows keep their model
# id forever: cost is computed at usage time with the price in force then
# (user principle — price changes must never rewrite history), and the read
# path prices pre-snapshot legacy rows against THIS table (plus MODEL_PRICING)
# instead of the live registry, so a retirement does not drop historical rows
# from cost. Provenance per entry: the rate + the commit/date that retired it.
RETIRED_MODEL_PRICING: dict[str, tuple[float, float, float]] = {
    # gemini-3.6-flash — retired 2026-08-13 (swap to gemini-3.7-flash, GA;
    # commit 06c593a45). Final standard rate $1.50 / $7.50 per 1M, cache read
    # 0.1x input ($0.15) — the 3.7 entry carries the same post-intro rate.
    "gemini-3.6-flash": (1.5, 0.15, 7.5),
    # kimi-k2.7-code — removed from the registry 2026-08-04 (revert #850,
    # commits 630fe6b65/bc60f0151). Official ¥6.50 / ¥1.30 / ¥27.00 per 1M
    # converted at ~6.77 CNY/USD (the price the registry entry carried).
    "kimi-k2.7-code": (0.96, 0.19, 3.99),
}


def _aware_utc(instant: datetime | None) -> datetime:
    if instant is None:
        return datetime.now(UTC)
    if instant.utcoffset() is None:
        raise ValueError("pricing instant must be timezone-aware")
    return instant.astimezone(UTC)


def rates_at(
    model: str,
    at: datetime | None = None,
    input_tokens: int | None = None,
) -> Rates | None:
    """Select rates for one model, instant, and total input-token tier.

    Effective intervals and UTC daily windows are half-open. Token-tier
    maxima are inclusive, mirroring provider wording such as ``<=200K``.
    ``input_tokens=None`` selects the base tier for catalog/display callers.
    Unknown models or an uncovered effective interval return ``None``.
    """
    if input_tokens is not None and input_tokens < 0:
        raise ValueError("input_tokens must be non-negative")
    instant = _aware_utc(at)
    entry = _CATALOG.get(model)
    if entry is not None:
        for period in entry.periods:
            if not period.contains(instant):
                continue
            for tier in period.tiers:
                if tier.contains(input_tokens):
                    return tier.rates_at(instant)
            return None
        return None
    plugin_price = _PLUGIN_PRICES.get(model)
    if plugin_price is not None:
        return plugin_price.rates
    retired = RETIRED_MODEL_PRICING.get(model)
    return Rates(*retired) if retired is not None else None


class _CurrentPricing(Mapping[str, tuple[float, float, float]]):
    """Compatibility mapping whose values follow the current UTC schedule.

    Iterates the catalog plus registered plugin prices — a plugin model must
    be visible wherever MODEL_PRICING is enumerated (e.g. the usage view).
    """

    def __getitem__(self, model: str) -> tuple[float, float, float]:
        selected = rates_at(model)
        if selected is None or (model not in _CATALOG and model not in _PLUGIN_PRICES):
            raise KeyError(model)
        return selected.as_tuple()

    def __iter__(self) -> Iterator[str]:
        return iter([*_CATALOG, *_PLUGIN_PRICES])

    def __len__(self) -> int:
        return len(_CATALOG) + len(_PLUGIN_PRICES)


MODEL_PRICING: Mapping[str, tuple[float, float, float]] = _CurrentPricing()


def tally_tokens(
    messages: Sequence[AnyMessage],
) -> tuple[int | None, int | None, int | None]:
    """Accumulate AIMessage.usage_metadata input/output/cache_read tokens.

    Returns `(in, out, cached)` triple. `in` includes `cached`
    (LangChain semantics: input_tokens is the total input;
    input_token_details.cache_read is the cache-hit portion;
    cache miss = in - cached).

    If no AIMessage has usage_metadata (extremely rare: mock LLM /
    old version), returns `(None, None, None)` instead of (0, 0, 0)
    — distinguishes "unknown" from "0 tokens"; cost_usd also returns
    None, so JSONL clearly shows data missing rather than a free call.
    """
    tok_in = 0
    tok_out = 0
    tok_cached = 0
    seen = False
    for m in messages:
        if not isinstance(m, AIMessage):
            continue
        meta = m.usage_metadata
        if not meta:
            continue
        seen = True
        tok_in += meta["input_tokens"]
        tok_out += meta["output_tokens"]
        details = meta.get("input_token_details") or {}
        tok_cached += int(details.get("cache_read", 0))
    if not seen:
        return None, None, None
    return tok_in, tok_out, tok_cached


def quote(
    model: str,
    tok_in: int | None,
    tok_out: int | None,
    tok_cached: int | None = None,
    *,
    at: datetime | None = None,
) -> CostQuote | None:
    """Price one call and return the selected rates with the cost.

    When `tok_cached=None`, fall back to 0 (treats all as cache miss,
    overestimate but safe); for callers with only old `(in, out)`
    data. Unknown tokens, models, or effective intervals return ``None``.
    """
    if tok_in is None or tok_out is None:
        return None
    cache_read = 0 if tok_cached is None else tok_cached
    if tok_in < 0 or tok_out < 0 or cache_read < 0 or cache_read > tok_in:
        raise ValueError("token counts must be non-negative and cached tokens cannot exceed input")
    selected = rates_at(model, at, tok_in)
    if selected is None:
        return None
    cache_miss = tok_in - cache_read
    cost = (
        cache_miss * selected.cache_miss
        + cache_read * selected.cache_hit
        + tok_out * selected.output
    ) / 1_000_000
    return CostQuote(cost_usd=cost, rates=selected)


def cost_usd(
    model: str,
    tok_in: int | None,
    tok_out: int | None,
    tok_cached: int | None = None,
    *,
    at: datetime | None = None,
) -> float | None:
    """Compatibility wrapper returning only ``quote(...).cost_usd``.

    The original four positional arguments retain their meaning. ``at`` is an
    optional keyword-only extension for deterministic historical/boundary
    pricing; omitting it selects the schedule at the current UTC instant.
    """
    priced = quote(model, tok_in, tok_out, tok_cached, at=at)
    return priced.cost_usd if priced is not None else None
