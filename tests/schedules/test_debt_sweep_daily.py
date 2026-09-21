"""Contract tests for the daily tech-debt clearing schedule."""

from __future__ import annotations

import importlib.util
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest

from ava.agents import AgentStatus as S
from shared.config import settings

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEDULE_PATH = REPO_ROOT / "schedules" / "debt-sweep-daily-schedule.py"


def _load_schedule_module() -> ModuleType:
    assert SCHEDULE_PATH.is_file()
    spec = importlib.util.spec_from_file_location("debt_sweep_daily", SCHEDULE_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(spec.name, None)
    return module


def _agent(agent_id: int, status: S, label: str = "debt-sweep-daily") -> SimpleNamespace:
    return SimpleNamespace(agent_id=agent_id, status=status, label=label)


def test_dry_run_performs_no_agent_claim_database_or_telemetry_side_effects(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    module = _load_schedule_module()
    calls: list[str] = []

    def forbidden(*args: object, **kwargs: object) -> None:
        del args, kwargs
        calls.append("side-effect")
        raise AssertionError("dry run made a side effect")

    def fake_scan(repo: Path, artifact_path: Path | None) -> Any:
        assert artifact_path is None
        return module.ScanReport(
            succeeded=True,
            artifact_path=tmp_path / "would-be-written.txt",
            summary="mechanical scan completed (3 lines)",
        )

    monkeypatch.setattr(module, "_run_mechanical_scan", fake_scan)
    monkeypatch.setattr(module.ava.agents, "list_agents", forbidden)
    monkeypatch.setattr(module.ava.agents, "spawn", forbidden)
    monkeypatch.setattr(module.ava.agents, "resurrect", forbidden)
    monkeypatch.setattr(module.ava.agents, "send_message", forbidden)
    monkeypatch.setattr(module, "catch_up", forbidden)
    monkeypatch.setattr(module, "fire_slot_once", forbidden)
    monkeypatch.setattr(module, "ava_home", forbidden)
    monkeypatch.setattr("shared.db.connect", forbidden)
    monkeypatch.setattr("shared.db.pool", forbidden)
    monkeypatch.setattr("shared.telemetry.emit", forbidden)

    module.main(["--once", "--dry-run", "--repo", str(REPO_ROOT)])

    assert calls == []
    output = capsys.readouterr().out
    assert "resolved worker label: debt-sweep-daily" in output
    assert "prompt preview:" in output
    assert "future/tech-debt/ledger.md" in output
    assert '"scan_status": "ok"' in output
    assert "no claims, agent operations, telemetry, or database access" in output


@pytest.mark.parametrize(
    ("agents", "expected_action"),
    [
        ([_agent(12, S.TERMINATED)], "resurrected"),
        ([_agent(13, S.IDLING)], "messaged"),
        ([], "spawned"),
    ],
)
def test_ensure_worker_reuses_or_spawns_by_status(
    monkeypatch: pytest.MonkeyPatch,
    agents: list[SimpleNamespace],
    expected_action: str,
) -> None:
    module = _load_schedule_module()
    calls: list[tuple[str, int | str]] = []

    def list_agents(**_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(agents=agents, next_cursor=None)

    def resurrect(agent_id: int, prompt: str) -> None:
        assert prompt == "clear debt"
        calls.append(("resurrect", agent_id))

    def send_message(agent_id: int, prompt: str) -> None:
        assert prompt == "clear debt"
        calls.append(("message", agent_id))

    def spawn(*, prompt: str, label: str) -> int:
        assert prompt == "clear debt"
        assert label == "debt-sweep-daily"
        calls.append(("spawn", label))
        return 14

    monkeypatch.setattr(module.ava.agents, "list_agents", list_agents)
    monkeypatch.setattr(module.ava.agents, "resurrect", resurrect)
    monkeypatch.setattr(module.ava.agents, "send_message", send_message)
    monkeypatch.setattr(module.ava.agents, "spawn", spawn)

    dispatch = module.ensure_worker("debt-sweep-daily", "clear debt")

    assert dispatch.action == expected_action
    assert dispatch.agent_id == (agents[0].agent_id if agents else 14)
    if expected_action == "resurrected":
        assert calls == [("resurrect", 12)]
    elif expected_action == "messaged":
        assert calls == [("message", 13)]
    else:
        assert calls == [("spawn", "debt-sweep-daily")]


def test_worker_prompt_names_ledger_scan_artifact_and_one_pr(tmp_path: Path) -> None:
    module = _load_schedule_module()
    scan = module.ScanReport(
        succeeded=False,
        artifact_path=tmp_path / "debt-scan.txt",
        summary="mechanical scan failed: exit 2",
    )

    prompt = module.worker_prompt("2026-09-21", scan)

    assert "today's debt-clearing pass" in prompt
    assert "origin/main" in prompt
    assert "ava.skills.sweeper" in prompt
    assert "future/tech-debt/ledger.md" in prompt
    assert str(tmp_path / "debt-scan.txt") in prompt
    assert "mechanical scan failed: exit 2" in prompt
    assert "chore(sweeper): debt reconcile 2026-09-21" in prompt
    assert "end your own process" in prompt


def test_scan_failure_is_passed_to_worker_and_registered(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = _load_schedule_module()
    scan = module.ScanReport(
        succeeded=False,
        artifact_path=tmp_path / "failed-scan.txt",
        summary="mechanical scan failed: exit 1",
    )
    prompts: list[str] = []
    emitted: list[dict[str, Any]] = []

    monkeypatch.setattr(module, "claimed_slot", lambda: datetime(2026, 9, 21, 22, 30, tzinfo=UTC))
    monkeypatch.setattr(module, "TZ", "Asia/Shanghai")
    monkeypatch.setattr(module, "_run_mechanical_scan", lambda *_args: scan)
    monkeypatch.setattr(
        module,
        "ensure_worker",
        lambda _label, prompt: (
            prompts.append(prompt) or module.WorkerDispatch(agent_id=44, action="spawned")
        ),
    )
    monkeypatch.setattr(module, "init_gateway_process", lambda **_kwargs: None)
    monkeypatch.setattr(
        "shared.telemetry.emit",
        lambda category, event_name, **kwargs: emitted.append(
            {"category": category, "event_name": event_name, **kwargs}
        ),
    )

    module._fire(None)

    assert len(prompts) == 1
    assert "mechanical scan failed: exit 1" in prompts[0]
    assert (
        emitted
        == [
            {
                "category": "telemetry",
                "event_name": "debt_sweep_daily",
                "source": "system",
                "attributes": {
                    "day": "2026-09-22",  # time-bomb-ok: derived from the pinned claimed_slot fixture (Asia/Shanghai), no real-clock window
                    "scan_status": "failed",
                    "action": "spawned",
                    "worker_agent_id": 44,
                },
            }
        ]
    )


def test_event_payload_and_cluster_clock_constants() -> None:
    module = _load_schedule_module()

    assert module.CRON == "30 6 * * *"
    assert settings.general.timezone == module.TZ
    assert module.event_payload(
        day="2026-09-21",  # time-bomb-ok: passthrough payload assertion, no clock-derived window
        scan=module.ScanReport(
            succeeded=True,
            artifact_path=Path("scan.txt"),
            summary="ok",
        ),
        dispatch=module.WorkerDispatch(agent_id=77, action="messaged"),
    ) == {
        "day": "2026-09-21",  # time-bomb-ok: passthrough payload assertion, no clock-derived window
        "scan_status": "ok",
        "action": "messaged",
        "worker_agent_id": 77,
    }


def test_ensure_worker_reuses_worker_from_later_search_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_schedule_module()
    unrelated = _agent(300, S.IDLING, label="steward-old")
    target = _agent(200, S.TERMINATED)
    pages = [
        SimpleNamespace(agents=[unrelated], next_cursor=300),
        SimpleNamespace(agents=[target], next_cursor=None),
    ]
    seen_before_ids: list[int | None] = []

    def list_agents(
        *, scope: str, query: str = "", before_id: int | None = None, limit: int = 100
    ) -> SimpleNamespace:
        seen_before_ids.append(before_id)
        return pages[len(seen_before_ids) - 1]

    resurrect = Mock()
    monkeypatch.setattr(module.ava.agents, "list_agents", list_agents)
    monkeypatch.setattr(module.ava.agents, "resurrect", resurrect)
    monkeypatch.setattr(module.ava.agents, "send_message", Mock())
    monkeypatch.setattr(module.ava.agents, "spawn", Mock())

    dispatch = module.ensure_worker("debt-sweep-daily", "clear debt")

    assert dispatch == module.WorkerDispatch(agent_id=200, action="resurrected")
    resurrect.assert_called_once_with(200, "clear debt")
    assert seen_before_ids == [None, 300]


def test_ensure_worker_spawns_only_after_matching_pages_are_exhausted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_schedule_module()
    pages = [
        SimpleNamespace(agents=[], next_cursor=300),
        SimpleNamespace(agents=[], next_cursor=None),
    ]
    seen_before_ids: list[int | None] = []

    def list_agents(
        *, scope: str, query: str = "", before_id: int | None = None, limit: int = 100
    ) -> SimpleNamespace:
        seen_before_ids.append(before_id)
        return pages[len(seen_before_ids) - 1]

    spawn = Mock(return_value=400)
    monkeypatch.setattr(module.ava.agents, "list_agents", list_agents)
    monkeypatch.setattr(module.ava.agents, "spawn", spawn)

    dispatch = module.ensure_worker("missing", "clear debt")

    assert dispatch == module.WorkerDispatch(agent_id=400, action="spawned")
    spawn.assert_called_once_with(prompt="clear debt", label="missing")
    assert seen_before_ids == [None, 300]


def test_runner_argv_passthrough_is_tolerated() -> None:
    """The runner runs this template in-process with its own argv
    (``python -m gateway.schedule_runner <id>``), so ``sys.argv[1:]`` carries
    the schedule id. Rejecting it exits 2 on every launch and trips the
    manager's crash breaker (observed live: schedule 22 crashed 5x)."""
    module = _load_schedule_module()
    parser = module.build_parser()

    runner = parser.parse_args(["22"])
    assert runner.schedule_id == 22
    assert not runner.once and not runner.dry_run

    demo = parser.parse_args(["22", "--once", "--dry-run", "--repo", "."])
    assert demo.once and demo.dry_run

    with pytest.raises(SystemExit):
        parser.parse_args(["--unknown-flag"])


def test_failure_notification_propagates_when_p0_lead_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_schedule_module()

    def unavailable() -> int:
        raise RuntimeError("P0 lead unavailable")

    monkeypatch.setattr(module, "_report_agent", unavailable)

    with pytest.raises(RuntimeError, match="P0 lead unavailable"):
        module._report_failure("worker dispatch failed")
