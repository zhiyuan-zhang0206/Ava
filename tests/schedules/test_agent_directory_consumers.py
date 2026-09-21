"""Schedule directory searches must traverse pages without duplicating role agents."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest

SCHEDULES = Path(__file__).resolve().parents[2] / "schedules"
ROLE_SCHEDULES = (
    "memory-steward-schedule.py",
    "self-evolution-daily-schedule.py",
    "self-evolution-weekly-schedule.py",
    "adversarial-eval-weekly-schedule.py",
)


def _load(filename: str) -> Any:
    spec = importlib.util.spec_from_file_location(
        "directory_" + filename.replace("-", "_").removesuffix(".py"), SCHEDULES / filename
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(spec.name, None)
    return module


def _directory(monkeypatch: pytest.MonkeyPatch, module: ModuleType, pages: list[Any]) -> Mock:
    # An explicit keyword signature makes calls to the removed list API fail.
    fetch = Mock(side_effect=pages)

    def list_agents(
        *, scope: str, query: str = "", before_id: int | None = None, limit: int = 100
    ) -> Any:
        return fetch(scope=scope, query=query, before_id=before_id, limit=limit)

    monkeypatch.setattr(module.ava.agents, "list_agents", list_agents)  # type: ignore[attr-defined]
    return fetch


@pytest.mark.parametrize("filename", ROLE_SCHEDULES)
def test_reuses_exact_role_from_later_search_page(
    filename: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _load(filename)
    unrelated = SimpleNamespace(agent_id=300, label="steward-old", status=module.S.IDLING)
    target = SimpleNamespace(agent_id=200, label="steward", status=module.S.TERMINATED)
    older = SimpleNamespace(agent_id=100, label="steward", status=module.S.IDLING)
    fetch = _directory(
        monkeypatch,
        module,
        [
            SimpleNamespace(agents=[unrelated], next_cursor=300),
            SimpleNamespace(agents=[target, older], next_cursor=100),
        ],
    )
    resurrect, send, spawn = Mock(), Mock(), Mock()
    monkeypatch.setattr(module.ava.agents, "resurrect", resurrect)
    monkeypatch.setattr(module.ava.agents, "send_message", send)
    monkeypatch.setattr(module.ava.agents, "spawn", spawn)

    assert module.ensure_agent("steward", "work") == 200
    resurrect.assert_called_once_with(200, "work")
    send.assert_not_called()
    spawn.assert_not_called()
    assert [call.kwargs["before_id"] for call in fetch.call_args_list] == [None, 300]
    assert all(call.kwargs["query"] == "steward" for call in fetch.call_args_list)


@pytest.mark.parametrize("filename", ROLE_SCHEDULES)
def test_spawns_only_after_all_matching_pages_are_exhausted(
    filename: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _load(filename)
    _directory(monkeypatch, module, [SimpleNamespace(agents=[], next_cursor=None)])
    spawn = Mock(return_value=400)
    monkeypatch.setattr(module.ava.agents, "spawn", spawn)

    assert module.ensure_agent("missing", "work") == 400
    spawn.assert_called_once_with(prompt="work", label="missing")


@pytest.mark.parametrize(
    "filename",
    (
        "c9-daily-report-schedule.py",
        "model-update-tracker-schedule.py",
        "debt-sweep-daily-schedule.py",
    ),
)
def test_report_recipient_can_be_beyond_first_search_page(
    filename: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _load(filename)
    monkeypatch.delenv(module._REPORT_AGENT_ENV, raising=False)
    target = SimpleNamespace(agent_id=200, label=module._REPORT_LABEL, status=module.S.IDLING)
    _directory(
        monkeypatch,
        module,
        [
            SimpleNamespace(agents=[], next_cursor=300),
            SimpleNamespace(agents=[target], next_cursor=None),
        ],
    )
    assert module._report_agent() == 200


def test_evaluation_explicitly_enumerates_the_complete_directory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load("adversarial-eval-weekly-schedule.py")
    rows = [SimpleNamespace(agent_id=300), SimpleNamespace(agent_id=200)]
    fetch = _directory(
        monkeypatch,
        module,
        [
            SimpleNamespace(agents=rows[:1], next_cursor=300),
            SimpleNamespace(agents=rows[1:], next_cursor=None),
        ],
    )
    agents = module._all_agents()
    fetch.assert_not_called()
    assert list(agents) == rows
    assert [call.kwargs["before_id"] for call in fetch.call_args_list] == [None, 300]


def test_known_workers_never_enumerate_the_directory(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load("adversarial-eval-weekly-schedule.py")
    fetch = _directory(monkeypatch, module, [])
    status = Mock(side_effect=[module.S.TERMINATED, module.S.IDLING])
    monkeypatch.setattr(module.ava.agents, "get_status", status)
    monkeypatch.setattr(module.ava.agents, "get_last_message", Mock(return_value="done"))
    assert module._wait_for_workers({10: "first", 20: "second"}) == set()
    assert {call.args[0] for call in status.call_args_list} == {10, 20}

    status.side_effect = [module.S.TERMINATED, module.S.RUNNING]
    terminate = Mock()
    monkeypatch.setattr(module.ava.agents, "terminate", terminate)
    module._terminate_live_agents({10, 20})
    terminate.assert_called_once_with(20, force=True)
    fetch.assert_not_called()


def test_worker_cleanup_searches_only_live_worker_labels(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load("adversarial-eval-weekly-schedule.py")
    fetch = _directory(
        monkeypatch,
        module,
        [
            SimpleNamespace(agents=[], next_cursor=300),
            SimpleNamespace(
                agents=[SimpleNamespace(agent_id=200, label=module.PROBE_LABEL)],
                next_cursor=None,
            ),
            SimpleNamespace(
                agents=[SimpleNamespace(agent_id=100, label=module.COLLEAGUE_LABEL)],
                next_cursor=None,
            ),
        ],
    )
    terminate = Mock()
    monkeypatch.setattr(module.ava.agents, "terminate", terminate)
    module._sweep_leftover_workers({100})
    terminate.assert_called_once_with(200, force=True)
    assert [call.kwargs["scope"] for call in fetch.call_args_list] == ["live"] * 3
    assert [call.kwargs["query"] for call in fetch.call_args_list] == [
        module.PROBE_LABEL,
        module.PROBE_LABEL,
        module.COLLEAGUE_LABEL,
    ]
    assert [call.kwargs["before_id"] for call in fetch.call_args_list] == [None, 300, None]
