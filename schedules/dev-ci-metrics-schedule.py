"""Daily host for the GitHub Actions run-observability sampler."""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import ava
import shared
from ava.agents import AgentStatus as S
from schedules.agent_status_guard import ensure_agent_status_members
from schedules.catchup import catch_up, claimed_slot, fire_slot_once
from shared.config import settings
from shared.watcher import next_fire

ensure_agent_status_members(S, {"IDLING", "RUNNING", "TERMINATED"}, schedule_name="dev-ci-metrics")

CRON = "20 6 * * *"
TZ = settings.general.timezone
_REPORT_AGENT_ENV = "AVA_CI_METRICS_REPORT_AGENT"
_REPORT_LABEL = "Ava \u8d1f\u8d23\u4eba"
_REPO_ROOT = Path(shared.__file__).resolve().parents[1]


def _report_agent() -> int:
    configured = os.environ.get(_REPORT_AGENT_ENV)
    if configured is not None and configured.strip():
        agent_id = int(configured)
        if agent_id <= 0:
            raise RuntimeError(f"{_REPORT_AGENT_ENV} must be a positive agent id")
        return agent_id
    before_id = None
    while True:
        page = ava.agents.list_agents(scope="all", query=_REPORT_LABEL, before_id=before_id)
        for agent in page.agents:
            if agent.label == _REPORT_LABEL and agent.status in (S.RUNNING, S.IDLING, S.TERMINATED):
                return agent.agent_id
        if page.next_cursor is None:
            raise RuntimeError(f"no report agent labelled {_REPORT_LABEL!r} is available")
        before_id = page.next_cursor


def _report_failure(detail: str) -> None:
    message = (
        f"Dev/CI metrics collection failed:\n{detail[-1000:]}\n"
        "Check the schedule log; backfill with `scripts/ci_runs_export.py --repo "
        "zhiyuan-zhang0206/Ava --print-snapshot`."
    )
    try:
        ava.agents.send_message(_report_agent(), message)
    except Exception as exc:
        print(f"dev-ci-metrics could not notify the P0 lead: {exc}")
        raise


def _load_exporter() -> Any:
    scripts_dir = _REPO_ROOT / "scripts"
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    return __import__("ci_runs_export")


def _snapshot(exporter: Any) -> dict[str, Any]:
    from shared.paths import ava_home

    path = ava_home() / exporter._STATE_DIR_RELATIVE / "snapshot.json"
    loaded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise TypeError("CI-run sampler wrote an invalid snapshot")
    return loaded


def _day_counts(snapshot: dict[str, Any], day: str, repo: str) -> tuple[int, int]:
    records = snapshot["repositories"][repo]["days"]
    values = records[day]
    return int(values["runs"]), int(values["failed_runs"])


def _run_exporter(exporter: Any, repo: str) -> None:
    if exporter.main(["--repo", repo]) != 0:
        raise RuntimeError("ci-runs-export exited non-zero")


def _fire(_payload: None) -> None:
    slot_end = claimed_slot()
    if slot_end is None:
        raise RuntimeError("dev-ci-metrics fired outside a claimed slot")
    try:
        exporter = _load_exporter()
        repo = exporter.DEFAULT_REPO
        _run_exporter(exporter, repo)
        day = (slot_end.astimezone(ZoneInfo(TZ)).date() - timedelta(days=1)).isoformat()
        runs, failed = _day_counts(_snapshot(exporter), day, repo)
        print(
            f"[{datetime.now(UTC).isoformat()}] dev-ci-metrics: {day} — {runs} runs, {failed} failed"
        )
    except Exception as exc:
        print(f"dev-ci-metrics failed: {exc}")
        _report_failure(f"{type(exc).__name__}: {exc}")


def _main_loop() -> None:
    catch_up([(CRON, None)], timezone=TZ, fire=_fire)
    last_run_at = datetime.now(UTC)
    while True:
        now = datetime.now(UTC)
        next_run = next_fire(CRON, after=now - timedelta(minutes=2), timezone=TZ)
        if next_run <= last_run_at:
            next_run = next_fire(CRON, after=last_run_at, timezone=TZ)
        delay = (next_run - now).total_seconds()
        if delay > 0:
            time.sleep(min(delay, 3600))
            continue
        fire_slot_once(next_run, None, fire=_fire)
        last_run_at = datetime.now(UTC)
        time.sleep(120)


if __name__ == "__main__":
    _main_loop()
