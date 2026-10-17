from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from scripts.model_registry import update_model_pricing as pricing_updater

_REPO_ROOT = Path(__file__).resolve().parents[2]


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


def _reviewed_catalog() -> dict[str, Any]:
    return json.loads((_REPO_ROOT / "base/lm/pricing/pricing_catalog_archive.json").read_text())


def _without_recorded_succession(catalog: dict[str, Any]) -> dict[str, Any]:
    periods = catalog["models"]["deepseek-v4-pro"]["periods"]
    periods.pop()
    periods[-1]["effective_until"] = None
    return catalog


def test_reconcile_is_a_noop_when_the_reviewed_catalog_matches() -> None:
    fetched = pricing_updater.parse_deepseek_pricing(_DEEPSEEK_TABLE)

    assert (
        pricing_updater.reconcile_deepseek_catalog(
            _reviewed_catalog(),
            fetched,
            detected_at="2026-09-11T12:34:56Z",
        )
        is None
    )


def test_reconcile_rejects_an_unexpected_column_roster() -> None:
    fetched = pricing_updater.parse_deepseek_pricing(
        _DEEPSEEK_TABLE.replace("deepseek-flash (1)", "deepseek-v4.1-flash (1)")
    )

    with pytest.raises(ValueError, match="must contain exactly"):
        pricing_updater.reconcile_deepseek_catalog(
            _reviewed_catalog(),
            fetched,
            detected_at="2026-09-11T12:34:56Z",
        )


def test_reconcile_appends_a_new_effective_period_without_rewriting_history() -> None:
    catalog = _reviewed_catalog()
    fetched = pricing_updater.parse_deepseek_pricing(
        _DEEPSEEK_TABLE.replace("<td>$1.2</td>", "<td>$1.28</td>").replace(
            "<td>$0.6</td>", "<td>$0.64</td>"
        )
    )

    updated = pricing_updater.reconcile_deepseek_catalog(
        catalog,
        fetched,
        detected_at="2026-09-15T12:34:56Z",
    )

    assert updated is not None
    for model in ("deepseek-flash", "deepseek-v4-flash", "deepseek-v4-flash-vision-exp"):
        periods = updated["models"][model]["periods"]
        assert periods[-2]["effective_until"] == "2026-09-15T12:34:56Z"
        assert periods[-1]["effective_from"] == "2026-09-15T12:34:56Z"
        assert periods[-1]["tiers"][0]["rates"]["output"] == "0.64"
        assert periods[-1]["tiers"][0]["utc_daily_overrides"][0]["rates"]["output"] == "1.28"
    assert catalog["models"]["deepseek-v4-flash"]["periods"][-1]["effective_until"] is None


def test_reconcile_appends_a_period_when_peak_windows_change() -> None:
    fetched = pricing_updater.parse_deepseek_pricing(
        _DEEPSEEK_TABLE.replace(
            "Peak hours are 01:00 - 04:00 and 06:00 - 10:00 UTC,",
            "Peak hours are 02:00 - 05:00 and 07:00 - 11:00 UTC,",
        )
    )

    updated = pricing_updater.reconcile_deepseek_catalog(
        _reviewed_catalog(),
        fetched,
        detected_at="2026-09-15T12:34:56Z",
    )

    assert updated is not None
    overrides = updated["models"]["deepseek-v4-flash"]["periods"][-1]["tiers"][0][
        "utc_daily_overrides"
    ]
    assert [(item["start"], item["end"]) for item in overrides] == [
        ("02:00:00", "05:00:00"),
        ("07:00:00", "11:00:00"),
    ]


def test_reconcile_records_the_pro_succession_as_a_future_period() -> None:
    reviewed = _reviewed_catalog()
    frozen = deepcopy(reviewed["models"]["deepseek-v4-pro"]["periods"][:-1])
    catalog = _without_recorded_succession(_reviewed_catalog())
    fetched = pricing_updater.parse_deepseek_pricing(_DEEPSEEK_TABLE)

    updated = pricing_updater.reconcile_deepseek_catalog(
        catalog,
        fetched,
        detected_at="2026-09-11T12:34:56Z",
    )

    assert updated is not None
    periods = updated["models"]["deepseek-v4-pro"]["periods"]
    assert periods[:-1] == frozen
    assert periods[-2]["effective_until"] == "2026-09-14T04:00:00Z"
    assert periods[-1]["effective_from"] == "2026-09-14T04:00:00Z"
    assert periods[-1]["tiers"][0]["rates"] == {
        "input": "0.15",
        "cache_read": "0.003",
        "output": "0.6",
    }
    assert [
        (item["start"], item["end"]) for item in periods[-1]["tiers"][0]["utc_daily_overrides"]
    ] == [("01:00:00", "04:00:00"), ("06:00:00", "10:00:00")]

    assert (
        pricing_updater.reconcile_deepseek_catalog(
            updated,
            fetched,
            detected_at="2026-09-11T12:34:56Z",
        )
        is None
    )


def test_reconcile_fails_closed_when_a_retired_column_changes() -> None:
    fetched = pricing_updater.parse_deepseek_pricing(
        _DEEPSEEK_TABLE.replace("<td>$0.022</td>", "<td>$0.024</td>").replace(
            "<td>$0.044</td>", "<td>$0.048</td>"
        )
    )

    with pytest.raises(ValueError, match="retired"):
        pricing_updater.reconcile_deepseek_catalog(
            _reviewed_catalog(),
            fetched,
            detected_at="2026-09-11T12:34:56Z",
        )


def test_reconcile_fails_closed_when_a_retired_column_drifts_before_its_succession() -> None:
    fetched = pricing_updater.parse_deepseek_pricing(
        _DEEPSEEK_TABLE.replace("<td>$0.022</td>", "<td>$0.024</td>").replace(
            "<td>$0.044</td>", "<td>$0.048</td>"
        )
    )

    with pytest.raises(ValueError, match="before its recorded retirement"):
        pricing_updater.reconcile_deepseek_catalog(
            _without_recorded_succession(_reviewed_catalog()),
            fetched,
            detected_at="2026-09-11T12:34:56Z",
        )


def test_workflow_runs_only_trusted_main_code_with_write_permissions() -> None:
    workflow = (_REPO_ROOT / ".github/workflows/update-model-pricing.yml").read_text()

    assert "if: github.ref == 'refs/heads/main'" in workflow
    assert "ref: main" in workflow
    assert 'git worktree add -B "$BRANCH" "$CANDIDATE"' in workflow
    assert '[ -L "$ARCHIVE" ]' in workflow
    assert 'ARCHIVE_REAL="$(realpath "$ARCHIVE")"' in workflow
    assert '"$CANDIDATE_REAL"/*' in workflow
    assert (
        'python scripts/model_registry/update_model_pricing.py --catalog "$ARCHIVE" --write'
        in workflow
    )
    assert (
        "python scripts/model_registry/update_model_pricing.py --sync-plugins --write" in workflow
    )
    assert "meiva_builtins/plugins/lm_*/provider.py" in workflow
    assert "bot sync → human review of the PR" in workflow
    assert "model-pricing-future-windows.md" in workflow
    assert 'cd "$CANDIDATE"' not in workflow
    assert "git checkout" not in workflow
