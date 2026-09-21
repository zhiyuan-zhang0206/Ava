"""Contract tests for the daily Dev/CI metrics schedule."""

from __future__ import annotations

import importlib.util
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_SCHEDULE = _ROOT / "schedules" / "dev-ci-metrics-schedule.py"


def _load() -> object:
    spec = importlib.util.spec_from_file_location("dev_ci_metrics", _SCHEDULE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(spec.name, None)
    return module


def test_fire_runs_default_repo_and_logs_previous_complete_day(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    module = _load()
    calls: list[list[str]] = []
    exporter = SimpleNamespace(DEFAULT_REPO="owner/repo", main=lambda args: calls.append(args) or 0)
    monkeypatch.setattr(module, "claimed_slot", lambda: datetime(2026, 9, 3, 22, 20, tzinfo=UTC))
    monkeypatch.setattr(module, "_load_exporter", lambda: exporter)
    monkeypatch.setattr(
        module,
        "_snapshot",
        lambda _exporter: {
            "repositories": {"owner/repo": {"days": {"2026-09-03": {"runs": 7, "failed_runs": 2}}}}
        },
    )

    module._fire(None)

    assert calls == [["--repo", "owner/repo"]]
    assert "2026-09-03 — 7 runs, 2 failed" in capsys.readouterr().out


def test_fire_reports_failed_collector(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load()
    monkeypatch.setattr(module, "claimed_slot", lambda: datetime(2026, 9, 3, 22, 20, tzinfo=UTC))
    monkeypatch.setattr(
        module,
        "_load_exporter",
        lambda: SimpleNamespace(DEFAULT_REPO="owner/repo", main=lambda _: 1),
    )
    failures: list[str] = []
    monkeypatch.setattr(module, "_report_failure", failures.append)

    module._fire(None)

    assert len(failures) == 1
    assert "exited non-zero" in failures[0]
    assert module.CRON == "20 6 * * *"
