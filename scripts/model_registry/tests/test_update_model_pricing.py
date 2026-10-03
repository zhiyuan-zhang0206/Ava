from __future__ import annotations

import importlib.util
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from scripts.model_registry import plugin_price_sync

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SCRIPT = _REPO_ROOT / "scripts" / "model_registry" / "update_model_pricing.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("update_model_pricing", _SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


pricing_updater = _load_script()

_DEEPSEEK_TABLE = """
<html><main>
<table>
  <tr><td colspan="3">MODEL</td><td>deepseek-flash (1)</td><td>deepseek-v4-pro (2)</td></tr>
  <tr><td colspan="3">MODEL VERSION</td><td>DeepSeek-V4.1-Flash</td><td>DeepSeek-V4-Pro-0813</td></tr>
  <tr><td rowspan="6">PRICING (3)</td><td rowspan="2">1M INPUT TOKENS (CACHE HIT)</td><td>OFF-PEAK</td><td>$0.003</td><td>$0.022</td></tr>
  <tr><td>PEAK</td><td>$0.006</td><td>$0.044</td></tr>
  <tr><td rowspan="2">1M INPUT TOKENS (CACHE MISS)</td><td>OFF-PEAK</td><td>$0.15</td><td>$0.66</td></tr>
  <tr><td>PEAK</td><td>$0.3</td><td>$1.32</td></tr>
  <tr><td rowspan="2">1M OUTPUT TOKENS</td><td>OFF-PEAK</td><td>$0.6</td><td>$1.98</td></tr>
  <tr><td>PEAK</td><td>$1.2</td><td>$3.96</td></tr>
</table>
<p>Off-peak rates are half of the peak rates. Peak hours are 01:00 - 04:00 and 06:00 - 10:00 UTC, Monday through Friday (all other hours are off-peak).</p>
<p>(1) Use deepseek-flash as the model name. The legacy names deepseek-v4-flash and deepseek-v4-flash-vision-exp are still accepted, but the corresponding models have been retired, their requests are served by the DeepSeek-V4.1-Flash model and billed at the Flash price.</p>
<p>(2) After 12:00 Beijing Time on September 14, 2026, requests to deepseek-v4-pro will all be routed to V4.1 Flash and billed at the V4.1 Flash price.</p>
</main></html>
"""

_PLUGIN_SOURCE = """from base.lm.provider_api import (
    PricePeriod,
    PriceRates,
    PriceTier,
    PriceWindow,
    ProviderContribution,
)

PROVIDER = ProviderContribution(
    binding=None,
    models={"fixture-model": None},
    pricing={
        "fixture-model": PriceRates(
            cache_miss=1.0,
            cache_hit=0.1,
            output=3.0,
            source_url="https://example.com/pricing",
            source_checked_at="2026-09-01",
            vendor="fixture",
        ),
    },
)
"""


def _sync_fixture(
    tmp_path: Path,
    *,
    current_rates: tuple[str, str, str],
    future_rates: tuple[str, str, str] | None = None,
    current_upper_rates: tuple[str, str, str] | None = None,
    current_windows: list[dict[str, Any]] | None = None,
    plugin_checked_at: str = "2026-09-01",
) -> tuple[Path, Path, Path]:
    repo_root = tmp_path / "repo"
    provider_path = repo_root / "ava_builtins/plugins/lm_fixture/provider.py"
    provider_path.parent.mkdir(parents=True)
    provider_path.write_text(
        _PLUGIN_SOURCE.replace("2026-09-01", plugin_checked_at),
        encoding="utf-8",
    )
    current_tiers: list[dict[str, Any]] = [
        {
            "input_tokens_min": 0,
            "input_tokens_max": 200_000 if current_upper_rates else None,
            "rates": {
                "input": current_rates[0],
                "cache_read": current_rates[1],
                "output": current_rates[2],
            },
            "utc_daily_overrides": current_windows or [],
        }
    ]
    if current_upper_rates is not None:
        current_tiers.append(
            {
                "input_tokens_min": 200_001,
                "input_tokens_max": None,
                "rates": {
                    "input": current_upper_rates[0],
                    "cache_read": current_upper_rates[1],
                    "output": current_upper_rates[2],
                },
                "utc_daily_overrides": [],
            }
        )
    periods: list[dict[str, Any]] = [
        {
            "effective_from": None,
            "effective_until": "2027-01-01T00:00:00Z" if future_rates else None,
            "tiers": current_tiers,
        }
    ]
    if future_rates is not None:
        periods.append(
            {
                "effective_from": "2027-01-01T00:00:00Z",
                "effective_until": None,
                "tiers": [
                    {
                        "input_tokens_min": 0,
                        "input_tokens_max": None,
                        "rates": {
                            "input": future_rates[0],
                            "cache_read": future_rates[1],
                            "output": future_rates[2],
                        },
                        "utc_daily_overrides": [],
                    }
                ],
            }
        )
    archive_path = repo_root / "base/lm/pricing_catalog_archive.json"
    archive_path.parent.mkdir(parents=True)
    archive_path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "catalog_version": "fixture",
                "currency": "USD",
                "unit_tokens": 1_000_000,
                "models": {
                    "fixture-model": {
                        "vendor": "fixture",
                        "source_url": "https://example.com/pricing",
                        "source_checked_at": "2026-09-05",
                        "periods": periods,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    return repo_root, archive_path, provider_path


def test_deepseek_parser_preserves_models_meters_and_decimal_units() -> None:
    catalog = pricing_updater.parse_deepseek_pricing(_DEEPSEEK_TABLE)
    prices = catalog.models

    assert catalog.peak_windows == (("01:00:00", "04:00:00"), ("06:00:00", "10:00:00"))
    # Column labels carry footnote markers; parsing yields the official model ids.
    assert list(prices) == ["deepseek-flash", "deepseek-v4-pro"]

    assert prices["deepseek-flash"].peak == pricing_updater.Rates(
        input=Decimal("0.3"),
        cache_read=Decimal("0.006"),
        output=Decimal("1.2"),
    )
    assert prices["deepseek-flash"].off_peak == pricing_updater.Rates(
        input=Decimal("0.15"),
        cache_read=Decimal("0.003"),
        output=Decimal("0.6"),
    )
    assert prices["deepseek-v4-pro"].peak == pricing_updater.Rates(
        input=Decimal("1.32"),
        cache_read=Decimal("0.044"),
        output=Decimal("3.96"),
    )
    assert prices["deepseek-v4-pro"].off_peak == pricing_updater.Rates(
        input=Decimal("0.66"),
        cache_read=Decimal("0.022"),
        output=Decimal("1.98"),
    )


def test_deepseek_parser_strips_column_footnote_markers() -> None:
    """The 2026-09-10 page labels its columns `deepseek-flash (1)`; the marker
    must never leak into a model id (that mismatch failed the daily workflow)."""
    marked = pricing_updater.parse_deepseek_pricing(_DEEPSEEK_TABLE)
    unmarked = pricing_updater.parse_deepseek_pricing(
        _DEEPSEEK_TABLE.replace("deepseek-flash (1)", "deepseek-flash")
    )

    assert set(marked.models) == {"deepseek-flash", "deepseek-v4-pro"}
    assert marked == unmarked


def test_deepseek_parser_rejects_an_empty_column_label() -> None:
    html = _DEEPSEEK_TABLE.replace("deepseek-v4-pro (2)", "(2)")

    with pytest.raises(ValueError, match="empty model id"):
        pricing_updater.parse_deepseek_pricing(html)


def test_deepseek_parser_fails_closed_when_a_meter_disappears() -> None:
    html = _DEEPSEEK_TABLE.replace(
        '<tr><td rowspan="2">1M OUTPUT TOKENS</td><td>OFF-PEAK</td><td>$0.6</td><td>$1.98</td></tr>',
        "",
    )

    with pytest.raises(ValueError, match="OUTPUT"):
        pricing_updater.parse_deepseek_pricing(html)


def test_deepseek_parser_rejects_an_unannounced_peak_ratio() -> None:
    html = _DEEPSEEK_TABLE.replace("$3.96", "$3.95")

    with pytest.raises(ValueError, match="half of peak"):
        pricing_updater.parse_deepseek_pricing(html)


def test_deepseek_parser_rejects_a_missing_peak_hour_statement() -> None:
    html = _DEEPSEEK_TABLE.replace(
        "Peak hours are 01:00 - 04:00 and 06:00 - 10:00 UTC,",
        "Peak hours are documented elsewhere.",
    )

    with pytest.raises(ValueError, match="peak-hour"):
        pricing_updater.parse_deepseek_pricing(html)


@pytest.mark.parametrize("bad_price", ["0.003", "$Infinity"])
def test_deepseek_parser_requires_finite_dollar_prices(bad_price: str) -> None:
    html = _DEEPSEEK_TABLE.replace("<td>$0.003</td>", f"<td>{bad_price}</td>")

    with pytest.raises(ValueError, match="invalid USD"):
        pricing_updater.parse_deepseek_pricing(html)


def test_deepseek_parser_rejects_duplicate_meter_rows() -> None:
    duplicate = (
        '<tr><td rowspan="2">1M INPUT TOKENS (CACHE HIT)</td>'
        "<td>OFF-PEAK</td><td>$0.003</td><td>$0.022</td></tr>"
        "<tr><td>PEAK</td><td>$0.006</td><td>$0.044</td></tr>"
    )
    html = _DEEPSEEK_TABLE.replace("</table>", f"{duplicate}</table>")

    with pytest.raises(ValueError, match="duplicate CACHE_READ"):
        pricing_updater.parse_deepseek_pricing(html)


@pytest.mark.parametrize("band", ["OFF-PEAK", "STANDARD", "FLAT PRICE"])
def test_deepseek_parser_rejects_an_unknown_pricing_meter(band: str) -> None:
    unknown = (
        '<tr><td rowspan="2">1M REASONING TOKENS</td>'
        f"<td>{band}</td><td>$0.10</td><td>$0.10</td></tr>"
        "<tr><td>PEAK</td><td>$0.20</td><td>$0.20</td></tr>"
    )
    html = _DEEPSEEK_TABLE.replace("</table>", f"{unknown}</table>")

    with pytest.raises(ValueError, match="unknown DeepSeek pricing meter"):
        pricing_updater.parse_deepseek_pricing(html)


def test_plugin_sync_rewrites_drift_and_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root, archive_path, provider_path = _sync_fixture(
        tmp_path,
        current_rates=("2.0", "0.2", "6.0"),
        future_rates=("4.0", "0.4", "12.0"),
        current_upper_rates=("3.0", "0.3", "9.0"),
        current_windows=[
            {
                "start": "01:00:00",
                "end": "04:00:00",
                "rates": {"input": "4.0", "cache_read": "0.4", "output": "12.0"},
            }
        ],
    )
    monkeypatch.setattr(
        pricing_updater,
        "_now_utc",
        lambda: datetime(2026, 9, 5, tzinfo=UTC),
    )

    first = pricing_updater.sync_plugin_rates(archive_path, repo_root)
    rewritten = provider_path.read_text(encoding="utf-8")
    compile(rewritten, str(provider_path), "exec")

    assert first.drifted_models == ("fixture-model",)
    assert first.changed_files == (provider_path,)
    assert "cache_miss=2.0," in rewritten
    assert "cache_hit=0.2," in rewritten
    assert "output=6.0," in rewritten
    assert 'source_checked_at="2026-09-05",' in rewritten
    assert rewritten.count("PricePeriod(") == 2
    assert rewritten.count("PriceTier(") == 3
    assert rewritten.count("PriceWindow(") == 1

    manifest = plugin_price_sync._provider_manifest(rewritten, provider_path)
    archive_entry = json.loads(archive_path.read_text())["models"]["fixture-model"]
    target, _upcoming = plugin_price_sync._archive_price(
        "fixture-model", archive_entry, datetime(2026, 9, 5, tzinfo=UTC)
    )
    assert manifest.prices["fixture-model"] == target

    second = pricing_updater.sync_plugin_rates(archive_path, repo_root)
    assert second.drifted_models == ()
    assert second.changed_files == ()
    assert provider_path.read_text(encoding="utf-8") == rewritten


def test_plugin_sync_leaves_full_matching_provider_byte_identical(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root, archive_path, provider_path = _sync_fixture(
        tmp_path,
        current_rates=("1.0", "0.1", "3.0"),
        plugin_checked_at="2026-09-05",
    )
    monkeypatch.setattr(
        pricing_updater,
        "_now_utc",
        lambda: datetime(2026, 9, 5, tzinfo=UTC),
    )
    pricing_updater.sync_plugin_rates(archive_path, repo_root)
    before = provider_path.read_bytes()

    result = pricing_updater.sync_plugin_rates(archive_path, repo_root)

    assert result.drifted_models == ()
    assert result.changed_files == ()
    assert provider_path.read_bytes() == before


def test_plugin_sync_check_exit_status_tracks_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root, archive_path, provider_path = _sync_fixture(
        tmp_path,
        current_rates=("2.0", "0.2", "6.0"),
    )
    monkeypatch.setattr(
        pricing_updater,
        "_now_utc",
        lambda: datetime(2026, 9, 5, tzinfo=UTC),
    )
    args = [
        "--sync-plugins",
        "--check",
        "--catalog",
        str(archive_path),
        "--repo-root",
        str(repo_root),
    ]

    assert pricing_updater.main(args) == 1
    assert provider_path.read_text(encoding="utf-8") == _PLUGIN_SOURCE

    pricing_updater.sync_plugin_rates(archive_path, repo_root)
    assert pricing_updater.main(args) == 0


def test_plugin_sync_reports_future_effective_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo_root, archive_path, _provider_path = _sync_fixture(
        tmp_path,
        current_rates=("1.0", "0.1", "3.0"),
        future_rates=("2.0", "0.2", "6.0"),
        plugin_checked_at="2026-09-05",
    )
    monkeypatch.setattr(
        pricing_updater,
        "_now_utc",
        lambda: datetime(2026, 9, 5, tzinfo=UTC),
    )

    pricing_updater.sync_plugin_rates(archive_path, repo_root, write=False)

    assert capsys.readouterr().err == (
        "- Plugin pricing drift: fixture-model: period 0 [None, "
        "'2027-01-01T00:00:00Z').\n"
        "- Plugin pricing drift: fixture-model: period 1 "
        "['2027-01-01T00:00:00Z', None).\n"
        "- `fixture-model` changes at `2027-01-01T00:00:00Z` to cache miss "
        "2.0, cache hit 0.2, output 6.0 USD/1M tokens.\n"
    )


def test_plugin_sync_check_reports_a_wrong_window_period(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo_root, archive_path, provider_path = _sync_fixture(
        tmp_path,
        current_rates=("2.0", "0.2", "6.0"),
        current_windows=[
            {
                "start": "01:00:00",
                "end": "04:00:00",
                "rates": {"input": "4.0", "cache_read": "0.4", "output": "12.0"},
            }
        ],
    )
    monkeypatch.setattr(
        pricing_updater,
        "_now_utc",
        lambda: datetime(2026, 9, 5, tzinfo=UTC),
    )
    pricing_updater.sync_plugin_rates(archive_path, repo_root)
    source = provider_path.read_text(encoding="utf-8")
    provider_path.write_text(
        source.replace('cache_miss="4.0"', 'cache_miss="4.1"', 1),
        encoding="utf-8",
    )

    result = pricing_updater.main(
        [
            "--sync-plugins",
            "--check",
            "--catalog",
            str(archive_path),
            "--repo-root",
            str(repo_root),
        ]
    )
    captured = capsys.readouterr()

    assert result == 1
    assert "fixture-model" in captured.out
    assert "fixture-model: period 0" in captured.err
