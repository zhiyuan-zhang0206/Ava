from __future__ import annotations

import importlib.util
import json
from decimal import Decimal
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO_ROOT / "scripts" / "update_model_pricing.py"


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
  <tr><td colspan="3">MODEL</td><td>deepseek-v4-flash</td><td>deepseek-v4-pro</td><td>deepseek-v4-flash-vision-exp</td></tr>
  <tr><td colspan="3">MODEL VERSION</td><td>DeepSeek-V4-Flash-0731</td><td>DeepSeek-V4-Pro-0813</td><td>DeepSeek-V4-Flash-Vision-Exp</td></tr>
  <tr><td rowspan="6">PRICING</td><td rowspan="2">1M INPUT TOKENS (CACHE HIT)</td><td>OFF-PEAK</td><td>$0.007</td><td>$0.022</td><td>$0.007</td></tr>
  <tr><td>PEAK</td><td>$0.014</td><td>$0.044</td><td>$0.014</td></tr>
  <tr><td rowspan="2">1M INPUT TOKENS (CACHE MISS)</td><td>OFF-PEAK</td><td>$0.22</td><td>$0.66</td><td>$0.22</td></tr>
  <tr><td>PEAK</td><td>$0.44</td><td>$1.32</td><td>$0.44</td></tr>
  <tr><td rowspan="2">1M OUTPUT TOKENS</td><td>OFF-PEAK</td><td>$0.66</td><td>$1.98</td><td>$0.66</td></tr>
  <tr><td>PEAK</td><td>$1.32</td><td>$3.96</td><td>$1.32</td></tr>
</table>
<p>Off-peak rates are half of the peak rates. Peak hours are 01:00 - 04:00 and 06:00 - 10:00 UTC.</p>
</main></html>
"""


def test_deepseek_parser_preserves_models_meters_and_decimal_units() -> None:
    catalog = pricing_updater.parse_deepseek_pricing(_DEEPSEEK_TABLE)
    prices = catalog.models

    assert catalog.peak_windows == (("01:00:00", "04:00:00"), ("06:00:00", "10:00:00"))

    assert prices["deepseek-v4-flash"].peak == pricing_updater.Rates(
        input=Decimal("0.44"),
        cache_read=Decimal("0.014"),
        output=Decimal("1.32"),
    )
    assert prices["deepseek-v4-flash"].off_peak == pricing_updater.Rates(
        input=Decimal("0.22"),
        cache_read=Decimal("0.007"),
        output=Decimal("0.66"),
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
    # The vision variant bills at v4-flash rates (its images count as input tokens).
    assert prices["deepseek-v4-flash-vision-exp"].peak == pricing_updater.Rates(
        input=Decimal("0.44"),
        cache_read=Decimal("0.014"),
        output=Decimal("1.32"),
    )
    assert prices["deepseek-v4-flash-vision-exp"].off_peak == pricing_updater.Rates(
        input=Decimal("0.22"),
        cache_read=Decimal("0.007"),
        output=Decimal("0.66"),
    )


def test_deepseek_parser_fails_closed_when_a_meter_disappears() -> None:
    html = _DEEPSEEK_TABLE.replace(
        '<tr><td rowspan="2">1M OUTPUT TOKENS</td><td>OFF-PEAK</td><td>$0.66</td><td>$1.98</td><td>$0.66</td></tr>',
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
        "Peak hours are 01:00 - 04:00 and 06:00 - 10:00 UTC.",
        "Peak hours are documented elsewhere.",
    )

    with pytest.raises(ValueError, match="peak-hour"):
        pricing_updater.parse_deepseek_pricing(html)


@pytest.mark.parametrize("bad_price", ["0.007", "$Infinity"])
def test_deepseek_parser_requires_finite_dollar_prices(bad_price: str) -> None:
    html = _DEEPSEEK_TABLE.replace("$0.007", bad_price)

    with pytest.raises(ValueError, match="invalid USD"):
        pricing_updater.parse_deepseek_pricing(html)


def test_deepseek_parser_rejects_duplicate_meter_rows() -> None:
    duplicate = (
        '<tr><td rowspan="2">1M INPUT TOKENS (CACHE HIT)</td>'
        "<td>OFF-PEAK</td><td>$0.007</td><td>$0.022</td></tr>"
        "<tr><td>PEAK</td><td>$0.014</td><td>$0.044</td></tr>"
    )
    html = _DEEPSEEK_TABLE.replace("</table>", f"{duplicate}</table>")

    with pytest.raises(ValueError, match="duplicate CACHE_READ"):
        pricing_updater.parse_deepseek_pricing(html)


@pytest.mark.parametrize("band", ["OFF-PEAK", "STANDARD", "FLAT PRICE"])
def test_deepseek_parser_rejects_an_unknown_pricing_meter(band: str) -> None:
    unknown = (
        '<tr><td rowspan="2">1M REASONING TOKENS</td>'
        f"<td>{band}</td><td>$0.10</td><td>$0.20</td></tr>"
        "<tr><td>PEAK</td><td>$0.20</td><td>$0.40</td></tr>"
    )
    html = _DEEPSEEK_TABLE.replace("</table>", f"{unknown}</table>")

    with pytest.raises(ValueError, match="unknown DeepSeek pricing meter"):
        pricing_updater.parse_deepseek_pricing(html)


def test_reconcile_is_a_noop_when_the_reviewed_catalog_matches() -> None:
    catalog = json.loads((_REPO_ROOT / "shared/lm/pricing_catalog_archive.json").read_text())
    fetched = pricing_updater.parse_deepseek_pricing(_DEEPSEEK_TABLE)

    assert (
        pricing_updater.reconcile_deepseek_catalog(
            catalog,
            fetched,
            detected_at="2026-08-18T12:34:56Z",
        )
        is None
    )


def test_reconcile_appends_a_new_effective_period_without_rewriting_history() -> None:
    catalog = json.loads((_REPO_ROOT / "shared/lm/pricing_catalog_archive.json").read_text())
    fetched = pricing_updater.parse_deepseek_pricing(
        _DEEPSEEK_TABLE.replace("$3.96", "$4.00").replace("$1.98", "$2.00")
    )

    updated = pricing_updater.reconcile_deepseek_catalog(
        catalog,
        fetched,
        detected_at="2026-08-19T12:34:56Z",
    )

    assert updated is not None
    periods = updated["models"]["deepseek-v4-pro"]["periods"]
    assert periods[-2]["effective_until"] == "2026-08-19T12:34:56Z"
    assert periods[-1]["effective_from"] == "2026-08-19T12:34:56Z"
    assert periods[-1]["tiers"][0]["rates"]["output"] == "2.00"
    assert periods[-1]["tiers"][0]["utc_daily_overrides"][0]["rates"]["output"] == "4.00"
    # Reconciliation works on a copy; a failed workflow cannot partially
    # mutate the catalog object its caller loaded.
    assert catalog["models"]["deepseek-v4-pro"]["periods"][-1]["effective_until"] is None


def test_reconcile_appends_a_period_when_peak_windows_change() -> None:
    catalog = json.loads((_REPO_ROOT / "shared/lm/pricing_catalog_archive.json").read_text())
    fetched = pricing_updater.parse_deepseek_pricing(
        _DEEPSEEK_TABLE.replace(
            "Peak hours are 01:00 - 04:00 and 06:00 - 10:00 UTC.",
            "Peak hours are 02:00 - 05:00 and 07:00 - 11:00 UTC.",
        )
    )

    updated = pricing_updater.reconcile_deepseek_catalog(
        catalog,
        fetched,
        detected_at="2026-08-19T12:34:56Z",
    )

    assert updated is not None
    overrides = updated["models"]["deepseek-v4-flash"]["periods"][-1]["tiers"][0][
        "utc_daily_overrides"
    ]
    assert [(item["start"], item["end"]) for item in overrides] == [
        ("02:00:00", "05:00:00"),
        ("07:00:00", "11:00:00"),
    ]


def test_workflow_runs_only_trusted_main_code_with_write_permissions() -> None:
    workflow = (_REPO_ROOT / ".github/workflows/update-model-pricing.yml").read_text()

    assert "if: github.ref == 'refs/heads/main'" in workflow
    assert "ref: main" in workflow
    assert 'git worktree add -B "$BRANCH" "$CANDIDATE"' in workflow
    assert '[ -L "$ARCHIVE" ]' in workflow
    assert 'ARCHIVE_REAL="$(realpath "$ARCHIVE")"' in workflow
    assert '"$CANDIDATE_REAL"/*' in workflow
    assert 'python scripts/update_model_pricing.py --catalog "$ARCHIVE" --write' in workflow
    assert "git checkout" not in workflow
