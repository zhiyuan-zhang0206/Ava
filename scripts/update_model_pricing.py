#!/usr/bin/env python3
"""Fetch official model pricing and reconcile it with Ava's catalog.

Provider adapters are intentionally strict and independent. A changed page
shape, missing model, unknown meter, or unit invariant is an error: automation
must never turn an upstream parsing mistake into a price used for billing.
"""

from __future__ import annotations

import argparse
import json
import re
from copy import deepcopy
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, NamedTuple

import requests
from bs4 import BeautifulSoup


class Rates(NamedTuple):
    """USD per one million input, cache-read, and output tokens."""

    input: Decimal
    cache_read: Decimal
    output: Decimal


class DeepSeekPrices(NamedTuple):
    """The two daily rate bands published for one DeepSeek model."""

    peak: Rates
    off_peak: Rates


class DeepSeekCatalog(NamedTuple):
    """All price and recurring-time facts parsed from the official page."""

    models: dict[str, DeepSeekPrices]
    peak_windows: tuple[tuple[str, str], ...]


_METERS = {
    "1M INPUT TOKENS (CACHE MISS)": "input",
    "1M INPUT TOKENS (CACHE HIT)": "cache_read",
    "1M OUTPUT TOKENS": "output",
}
_DEEPSEEK_MODELS = {"deepseek-v4-flash", "deepseek-v4-pro"}
_PEAK_HOURS = re.compile(
    r"\bPeak hours are\s+(\d{2}:\d{2})\s*-\s*(\d{2}:\d{2})\s+"
    r"and\s+(\d{2}:\d{2})\s*-\s*(\d{2}:\d{2})\s+UTC\b"
)
_USD = re.compile(r"\$(?:0|[1-9]\d*)(?:\.\d+)?")
_DEEPSEEK_PRICING_URL = "https://api-docs.deepseek.com/quick_start/pricing/"
_CATALOG_PATH = Path(__file__).resolve().parents[1] / "shared/lm/pricing_catalog.json"


def _usd(cell: str) -> Decimal:
    normalized = cell.strip()
    if _USD.fullmatch(normalized) is None:
        raise ValueError(f"invalid USD price: {cell!r}")
    try:
        value = Decimal(normalized[1:])
    except InvalidOperation as exc:
        raise ValueError(f"invalid USD price: {cell!r}") from exc
    if not value.is_finite() or value <= 0:
        raise ValueError(f"price must be positive: {cell!r}")
    return value


def parse_deepseek_pricing(html: str) -> DeepSeekCatalog:
    """Parse DeepSeek's official pricing table into exact USD/M rates.

    The table uses rowspans, so a peak row inherits the meter named by the
    preceding off-peak row. All three meters and the documented 50% off-peak
    relationship are required before any value is returned.
    """
    soup = BeautifulSoup(html, "lxml")
    page_text = soup.get_text(" ", strip=True)
    peak_hour_matches = _PEAK_HOURS.findall(page_text)
    if len(peak_hour_matches) != 1:
        raise ValueError("DeepSeek must publish exactly one recognized peak-hour statement")
    clocks = peak_hour_matches[0]
    peak_windows = tuple(
        (f"{clocks[index]}:00", f"{clocks[index + 1]}:00") for index in range(0, 4, 2)
    )
    rows = [
        [cell.get_text(" ", strip=True) for cell in row.select("th,td")]
        for row in soup.select("table tr")
    ]
    model_row = next((row for row in rows if row and row[0] == "MODEL"), None)
    if model_row is None or len(model_row) < 3:
        raise ValueError("DeepSeek MODEL row is missing or incomplete")
    models = model_row[1:]
    if len(models) != len(set(models)):
        raise ValueError("DeepSeek MODEL row contains duplicate model ids")

    bands: dict[str, dict[str, dict[str, Decimal]]] = {
        model: {"peak": {}, "off_peak": {}} for model in models
    }
    for index, row in enumerate(rows):
        meter_label = next((label for label in _METERS if label in row), None)
        if meter_label is None:
            continue
        if "OFF-PEAK" not in row:
            raise ValueError(f"{meter_label} row has no OFF-PEAK band")
        if index + 1 >= len(rows) or not rows[index + 1] or rows[index + 1][0] != "PEAK":
            raise ValueError(f"{meter_label} row has no following PEAK band")

        off_peak_cells = row[-len(models) :]
        peak_cells = rows[index + 1][-len(models) :]
        if len(off_peak_cells) != len(models) or len(peak_cells) != len(models):
            raise ValueError(f"{meter_label} price coverage does not match MODEL row")
        meter = _METERS[meter_label]
        for model, off_peak_cell, peak_cell in zip(models, off_peak_cells, peak_cells, strict=True):
            if meter in bands[model]["off_peak"] or meter in bands[model]["peak"]:
                raise ValueError(f"DeepSeek {model} contains duplicate {meter.upper()} pricing")
            bands[model]["off_peak"][meter] = _usd(off_peak_cell)
            bands[model]["peak"][meter] = _usd(peak_cell)

    parsed: dict[str, DeepSeekPrices] = {}
    for model, model_bands in bands.items():
        for band_name in ("peak", "off_peak"):
            missing = set(Rates._fields) - model_bands[band_name].keys()
            if missing:
                labels = ", ".join(sorted(name.upper() for name in missing))
                raise ValueError(f"DeepSeek {model} is missing {labels} pricing")
        peak = Rates(**model_bands["peak"])
        off_peak = Rates(**model_bands["off_peak"])
        if any(off * 2 != on for off, on in zip(off_peak, peak, strict=True)):
            raise ValueError(f"DeepSeek {model} off-peak prices are not half of peak prices")
        parsed[model] = DeepSeekPrices(peak=peak, off_peak=off_peak)
    return DeepSeekCatalog(models=parsed, peak_windows=peak_windows)


def _catalog_rates(raw: dict[str, str]) -> Rates:
    try:
        return Rates(
            input=Decimal(raw["input"]),
            cache_read=Decimal(raw["cache_read"]),
            output=Decimal(raw["output"]),
        )
    except (InvalidOperation, KeyError) as exc:
        raise ValueError("catalog rates must contain decimal input/cache_read/output") from exc


def _current_deepseek_prices(
    entry: dict[str, Any],
) -> tuple[DeepSeekPrices, tuple[tuple[str, str], ...]]:
    current = [period for period in entry["periods"] if period["effective_until"] is None]
    if len(current) != 1:
        raise ValueError("DeepSeek catalog entry must have exactly one current period")
    tiers = current[0]["tiers"]
    if (
        len(tiers) != 1
        or tiers[0]["input_tokens_min"] != 0
        or tiers[0]["input_tokens_max"] is not None
    ):
        raise ValueError("DeepSeek current pricing must have one unbounded token tier")
    tier = tiers[0]
    overrides = tier["utc_daily_overrides"]
    windows = tuple((override["start"], override["end"]) for override in overrides)
    if not windows:
        raise ValueError("DeepSeek current pricing must carry UTC peak windows")
    peak_rates = {_catalog_rates(override["rates"]) for override in overrides}
    if len(peak_rates) != 1:
        raise ValueError("DeepSeek UTC peak windows disagree on rates")
    prices = DeepSeekPrices(peak=peak_rates.pop(), off_peak=_catalog_rates(tier["rates"]))
    return prices, windows


def _rates_json(rates: Rates) -> dict[str, str]:
    return {
        "input": str(rates.input),
        "cache_read": str(rates.cache_read),
        "output": str(rates.output),
    }


def _new_deepseek_period(
    prices: DeepSeekPrices,
    effective_from: str,
    peak_windows: tuple[tuple[str, str], ...],
) -> dict[str, Any]:
    return {
        "effective_from": effective_from,
        "effective_until": None,
        "tiers": [
            {
                "input_tokens_min": 0,
                "input_tokens_max": None,
                "rates": _rates_json(prices.off_peak),
                "utc_daily_overrides": [
                    {"start": start, "end": end, "rates": _rates_json(prices.peak)}
                    for start, end in peak_windows
                ],
            }
        ],
    }


def reconcile_deepseek_catalog(
    catalog: dict[str, Any],
    fetched: DeepSeekCatalog,
    *,
    detected_at: str,
) -> dict[str, Any] | None:
    """Return a catalog copy with changed rates appended, or None if equal.

    `detected_at` is deliberately recorded as the provisional effective time.
    The generated PR must verify it against the provider announcement before
    merge; retaining the prior period makes that correction a one-field edit
    instead of rewriting price history.
    """
    instant = datetime.fromisoformat(detected_at.replace("Z", "+00:00"))
    if instant.tzinfo is None or instant.utcoffset() is None or not detected_at.endswith("Z"):
        raise ValueError("detected_at must be an ISO-8601 UTC instant ending in Z")
    if set(fetched.models) != _DEEPSEEK_MODELS:
        raise ValueError(f"DeepSeek source must contain exactly {sorted(_DEEPSEEK_MODELS)}")

    updated = deepcopy(catalog)
    changed = False
    for model in sorted(_DEEPSEEK_MODELS):
        entry = updated["models"][model]
        current_prices, current_windows = _current_deepseek_prices(entry)
        if current_prices == fetched.models[model] and current_windows == fetched.peak_windows:
            continue
        current = next(period for period in entry["periods"] if period["effective_until"] is None)
        current["effective_until"] = detected_at
        entry["periods"].append(
            _new_deepseek_period(fetched.models[model], detected_at, fetched.peak_windows)
        )
        entry["source_checked_at"] = detected_at[:10]
        changed = True

    if not changed:
        return None
    updated["catalog_version"] = detected_at
    return updated


def _detected_at_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _fetch_deepseek_html() -> str:
    response = requests.get(
        _DEEPSEEK_PRICING_URL,
        headers={"User-Agent": "Ava model-pricing updater"},
        timeout=30,
    )
    response.raise_for_status()
    return response.content.decode("utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Compare Ava's reviewed catalog with official provider pricing."
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="append detected changes to the catalog for a reviewable PR",
    )
    parser.add_argument(
        "--source-file",
        type=Path,
        help="read a captured DeepSeek HTML page instead of the live official URL",
    )
    parser.add_argument("--catalog", type=Path, default=_CATALOG_PATH)
    parser.add_argument(
        "--detected-at",
        default=None,
        help="provisional UTC effective instant (default: current time)",
    )
    args = parser.parse_args(argv)

    html = (
        args.source_file.read_text(encoding="utf-8")
        if args.source_file is not None
        else _fetch_deepseek_html()
    )
    catalog = json.loads(args.catalog.read_text(encoding="utf-8"))
    detected_at = args.detected_at or _detected_at_now()
    updated = reconcile_deepseek_catalog(
        catalog,
        parse_deepseek_pricing(html),
        detected_at=detected_at,
    )
    if updated is None:
        print("DeepSeek pricing matches the reviewed catalog.")
        return 0
    if not args.write:
        print(
            "DeepSeek pricing drift detected. Run with --write to append a provisional "
            "effective period, then verify its timestamp against the provider announcement."
        )
        return 1
    args.catalog.write_text(
        json.dumps(updated, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(f"Updated {args.catalog} at provisional effective time {detected_at}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
