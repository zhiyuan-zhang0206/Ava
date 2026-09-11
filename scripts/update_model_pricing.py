#!/usr/bin/env python3
"""Fetch official DeepSeek pricing and reconcile it with Ava's archive.

Provider adapters are intentionally strict and independent. A changed page
shape, missing model, unknown meter, or unit invariant is an error: automation
must never turn an upstream parsing mistake into a price used for billing.

The page prices three catalog entries through two columns: `deepseek-flash`
prices the retired deepseek-v4-flash / deepseek-v4-flash-vision-exp names
(DeepSeek keeps accepting and billing them at the Flash price), and
`deepseek-v4-pro` retires onto the Flash column at the page's published
instant, recorded as the entry's future period. Peak windows are the page's
daily UTC hours; the page scopes them Monday through Friday, so this ledger
bills weekend hours at peak rates — a known overestimate the archive's
daily-recurrence windows cannot express yet.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from copy import deepcopy
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, NamedTuple

import requests
from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.plugin_price_sync import PluginSyncResult
from scripts.plugin_price_sync import sync_plugin_rates as _sync_plugin_rates


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
# Official page columns → the catalog entries each column prices. Since the
# V4.1-Flash release (2026-09-10) three entries are priced through two
# columns: footnote (1) keeps the retired deepseek-v4-flash and
# deepseek-v4-flash-vision-exp names accepted and billed at the Flash price.
# deepseek-v4.1-flash-expires-on-0910 is deliberately NOT priced from the
# page: it is an internal beta the page never listed (announced 2026-09-08,
# same pricing as v4-flash), its rates are pinned manually in the plugin +
# archive ledger entries, and it expired 2026-09-10 — no auto-refresh is
# wired for a ~2-day model. An equality check below keeps the fetched page in
# lockstep with this roster.
_DEEPSEEK_COLUMNS: dict[str, tuple[str, ...]] = {
    "deepseek-flash": ("deepseek-v4-flash", "deepseek-v4-flash-vision-exp"),
    "deepseek-v4-pro": ("deepseek-v4-pro",),
}


class Succession(NamedTuple):
    """A page column whose model another column prices from an instant on."""

    successor: str
    effective_from: str


# Footnote (2): after 12:00 Beijing Time on 2026-09-14 (= 04:00 UTC) requests
# to deepseek-v4-pro are routed to V4.1 Flash and billed at the V4.1 Flash
# price. The reconcile records that succession as the entry's future period,
# closing the pro band at the published instant; afterwards the pro column is
# frozen history — nothing bills from it again, so the adapter never rewrites
# the retired entry.
_DEEPSEEK_SUCCESSION: dict[str, Succession] = {
    "deepseek-v4-pro": Succession(
        successor="deepseek-flash",
        effective_from="2026-09-14T04:00:00Z",
    ),
}
_PEAK_HOURS = re.compile(
    r"\bPeak hours are\s+(\d{2}:\d{2})\s*-\s*(\d{2}:\d{2})\s+"
    r"and\s+(\d{2}:\d{2})\s*-\s*(\d{2}:\d{2})\s+UTC\b"
)
_USD = re.compile(r"\$(?:0|[1-9]\d*)(?:\.\d+)?")
_MODEL_LABEL_FOOTNOTE = re.compile(r"\s*\(\d+\)$")
_DEEPSEEK_PRICING_URL = "https://api-docs.deepseek.com/quick_start/pricing/"
_CATALOG_PATH = Path(__file__).resolve().parents[1] / "shared/lm/pricing_catalog_archive.json"
_REPO_ROOT = Path(__file__).resolve().parents[1]


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
    preceding off-peak row, and column labels carry footnote markers
    (`deepseek-flash (1)`) that are stripped to the official model id. All
    three meters and the documented 50% off-peak relationship are required
    before any value is returned.
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
    models = [_MODEL_LABEL_FOOTNOTE.sub("", cell) for cell in model_row[1:]]
    if not all(models):
        raise ValueError("DeepSeek MODEL row contains an empty model id")
    if len(models) != len(set(models)):
        raise ValueError("DeepSeek MODEL row contains duplicate model ids")

    bands: dict[str, dict[str, dict[str, Decimal]]] = {
        model: {"peak": {}, "off_peak": {}} for model in models
    }
    for index, row in enumerate(rows):
        known_meters = [label for label in _METERS if label in row]
        token_cells = [cell for cell in row if "TOKEN" in cell.upper()]
        if token_cells and len(known_meters) != 1:
            raise ValueError(f"unknown DeepSeek pricing meter: {token_cells!r}")
        if not known_meters:
            continue
        meter_label = known_meters[0]
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


def _period_deepseek_prices(
    period: dict[str, Any],
) -> tuple[DeepSeekPrices, tuple[tuple[str, str], ...]]:
    """Read one catalog period back into fetched-shape prices and windows."""
    tiers = period["tiers"]
    if (
        len(tiers) != 1
        or tiers[0]["input_tokens_min"] != 0
        or tiers[0]["input_tokens_max"] is not None
    ):
        raise ValueError("DeepSeek pricing must have one unbounded token tier")
    tier = tiers[0]
    overrides = tier["utc_daily_overrides"]
    windows = tuple((override["start"], override["end"]) for override in overrides)
    if not windows:
        raise ValueError("DeepSeek pricing must carry UTC peak windows")
    peak_rates = {_catalog_rates(override["rates"]) for override in overrides}
    if len(peak_rates) != 1:
        raise ValueError("DeepSeek UTC peak windows disagree on rates")
    prices = DeepSeekPrices(peak=peak_rates.pop(), off_peak=_catalog_rates(tier["rates"]))
    return prices, windows


def _current_deepseek_prices(
    entry: dict[str, Any],
) -> tuple[DeepSeekPrices, tuple[tuple[str, str], ...]]:
    current = [period for period in entry["periods"] if period["effective_until"] is None]
    if len(current) != 1:
        raise ValueError("DeepSeek catalog entry must have exactly one current period")
    return _period_deepseek_prices(current[0])


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


def _append_reviewed_period(
    entry: dict[str, Any],
    prices: DeepSeekPrices,
    peak_windows: tuple[tuple[str, str], ...],
    detected_at: str,
) -> bool:
    """Append fetched rates as a new period when the open one differs."""
    current_prices, current_windows = _current_deepseek_prices(entry)
    if current_prices == prices and current_windows == peak_windows:
        return False
    current = next(period for period in entry["periods"] if period["effective_until"] is None)
    current["effective_until"] = detected_at
    entry["periods"].append(_new_deepseek_period(prices, detected_at, peak_windows))
    entry["source_checked_at"] = detected_at[:10]
    return True


def _record_succession(
    entry: dict[str, Any],
    fetched: DeepSeekCatalog,
    *,
    column: str,
    succession: Succession,
    detected_at: str,
) -> bool:
    """Close a retired column's band at its succession instant onto the successor's rates.

    Returns True when the successor period was appended. Once recorded the
    retired column is frozen history: its closed band's rates must keep
    matching the official column (any change fails closed for review; the peak
    windows are a global page property the live columns reconcile), and the
    successor period is never repriced — nothing bills from the retired column
    after the succession instant.
    """
    own_prices = fetched.models[column]
    periods = entry["periods"]
    last = periods[-1]
    if last["effective_from"] == succession.effective_from:
        if (
            last["effective_until"] is not None
            or len(periods) < 2
            or periods[-2]["effective_until"] != succession.effective_from
        ):
            raise ValueError(f"DeepSeek {column!r} succession periods are malformed")
        frozen_prices, _frozen_windows = _period_deepseek_prices(periods[-2])
        if frozen_prices != own_prices:
            raise ValueError(
                f"DeepSeek {column!r} is retired to {succession.successor!r}; its official "
                "column no longer matches the frozen period — review manually"
            )
        return False
    current_prices, _current_windows = _current_deepseek_prices(entry)
    if current_prices != own_prices:
        raise ValueError(
            f"DeepSeek {column!r} changed before its recorded retirement; review manually"
        )
    current = next(period for period in periods if period["effective_until"] is None)
    current["effective_until"] = succession.effective_from
    periods.append(
        _new_deepseek_period(
            fetched.models[succession.successor], succession.effective_from, fetched.peak_windows
        )
    )
    entry["source_checked_at"] = detected_at[:10]
    return True


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
    if set(fetched.models) != set(_DEEPSEEK_COLUMNS):
        raise ValueError(f"DeepSeek source must contain exactly {sorted(_DEEPSEEK_COLUMNS)}")

    updated = deepcopy(catalog)
    changed = False
    for column, models in sorted(_DEEPSEEK_COLUMNS.items()):
        succession = _DEEPSEEK_SUCCESSION.get(column)
        for model in models:
            entry = updated["models"][model]
            if succession is None:
                appended = _append_reviewed_period(
                    entry, fetched.models[column], fetched.peak_windows, detected_at
                )
            else:
                appended = _record_succession(
                    entry,
                    fetched,
                    column=column,
                    succession=succession,
                    detected_at=detected_at,
                )
            changed = changed or appended

    if not changed:
        return None
    updated["catalog_version"] = detected_at
    return updated


def _detected_at_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _now_utc() -> datetime:
    return datetime.now(UTC)


def sync_plugin_rates(
    archive_path: Path,
    repo_root: Path,
    *,
    write: bool = True,
) -> PluginSyncResult:
    """Synchronize built-in providers to the archive's complete price semantics."""
    return _sync_plugin_rates(archive_path, repo_root, now=_now_utc(), write=write)


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
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--write",
        action="store_true",
        help="append detected changes to the catalog for a reviewable PR",
    )
    mode.add_argument(
        "--check",
        action="store_true",
        help="report drift without writing and exit 1 when changes are needed",
    )
    parser.add_argument(
        "--sync-plugins",
        action="store_true",
        help="synchronize plugin PriceRates from the archive instead of fetching DeepSeek",
    )
    parser.add_argument(
        "--source-file",
        type=Path,
        help="read a captured DeepSeek HTML page instead of the live official URL",
    )
    parser.add_argument("--catalog", type=Path)
    parser.add_argument(
        "--repo-root",
        type=Path,
        help="repository whose built-in provider files are synchronized",
    )
    parser.add_argument(
        "--detected-at",
        default=None,
        help="provisional UTC effective instant (default: current time)",
    )
    args = parser.parse_args(argv)

    repo_root = args.repo_root or _REPO_ROOT
    catalog_path = args.catalog or (
        repo_root / "shared/lm/pricing_catalog_archive.json" if args.sync_plugins else _CATALOG_PATH
    )
    if args.sync_plugins:
        if args.source_file is not None or args.detected_at is not None:
            parser.error("--source-file/--detected-at cannot be used with --sync-plugins")
        result = sync_plugin_rates(catalog_path, repo_root, write=args.write)
        if result.drifted_models and not args.write:
            print(f"Plugin pricing drift detected for: {', '.join(result.drifted_models)}")
            return 1
        if result.changed_files:
            print(f"Synchronized plugin rates in {len(result.changed_files)} provider file(s).")
        else:
            print("Plugin prices match the archive's full period/tier/window semantics.")
        return 0

    html = (
        args.source_file.read_text(encoding="utf-8")
        if args.source_file is not None
        else _fetch_deepseek_html()
    )
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
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
    catalog_path.write_text(
        json.dumps(updated, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(f"Updated {catalog_path} at provisional effective time {detected_at}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
