"""Contract tests for the daily Dev/CI metrics schedule."""

from __future__ import annotations

import importlib.util
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_SCHEDULE = _ROOT / "schedules" / "dev-ci-metrics-schedule.py"


def _load() -> ModuleType:
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
    # Pin the cluster timezone: the day label must follow the cluster wall
    # clock, not the test environment's zone (CI has no cluster config).
    monkeypatch.setattr(module, "TZ", "Asia/Shanghai")
    monkeypatch.setattr(module, "claimed_slot", lambda: datetime(2026, 9, 3, 22, 20, tzinfo=UTC))
    monkeypatch.setattr(module, "_load_exporter", lambda: exporter)
    monkeypatch.setattr(
        module,
        "_snapshot",
        lambda _exporter: {
            "repositories": {"owner/repo": {"days": {"2026-09-03": {"runs": 7, "failed_runs": 2}}}}
        },
    )
    failures: list[str] = []
    monkeypatch.setattr(module, "_report_failure", failures.append)

    module._fire(None)

    assert calls == [["--repo", "owner/repo"]]
    assert "2026-09-03 — 7 runs, 2 failed" in capsys.readouterr().out
    assert failures == []


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


def test_report_agent_override_rejects_nonnumeric_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load()
    monkeypatch.setenv(module._REPORT_AGENT_ENV, "not-an-id")

    with pytest.raises(
        RuntimeError, match="AVA_CI_METRICS_REPORT_AGENT must be a numeric agent id"
    ):
        module._report_agent()


def test_report_failure_uses_exact_label_and_existing_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load()
    monkeypatch.delenv(module._REPORT_AGENT_ENV, raising=False)
    unrelated = SimpleNamespace(agent_id=31, label="another", status=module.S.RUNNING)
    target = SimpleNamespace(agent_id=17, label=module._REPORT_LABEL, status=module.S.IDLING)
    find = Mock(
        side_effect=[
            SimpleNamespace(agents=[unrelated], next_cursor=31),
            SimpleNamespace(agents=[target], next_cursor=None),
        ]
    )
    send = Mock()
    monkeypatch.setattr(module.ava.agents, "list_agents", find)
    monkeypatch.setattr(module.ava.agents, "send_message", send)

    module._report_failure("exporter failed")

    assert [call.kwargs for call in find.call_args_list] == [
        {"scope": "all", "query": module._REPORT_LABEL, "before_id": None},
        {"scope": "all", "query": module._REPORT_LABEL, "before_id": 31},
    ]
    send.assert_called_once_with(
        17,
        "Dev/CI metrics collection failed:\nexporter failed\n"
        "Check the schedule log; backfill with `scripts/ci_runs_export.py --repo "
        "zhiyuan-zhang0206/Ava --print-snapshot`.",
    )
