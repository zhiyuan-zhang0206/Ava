"""Daily host for the GitHub Actions run-observability sampler."""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import ava
import shared
from ava.agents import AgentStatus as S
from schedules.agent_status_guard import ensure_agent_status_members
from schedules.catchup import claimed_slot
from schedules.daily_host import report_agent, run_daily_loop
from shared.config import settings

ensure_agent_status_members(S, {"IDLING", "RUNNING", "TERMINATED"}, schedule_name="dev-ci-metrics")

CRON = "20 6 * * *"
TZ = settings.general.timezone
_REPORT_AGENT_ENV = "AVA_CI_METRICS_REPORT_AGENT"
_REPORT_LABEL = "Ava \u8d1f\u8d23\u4eba"
_REPO_ROOT = Path(shared.__file__).resolve().parents[1]


def _report_agent() -> int:
    return report_agent(_REPORT_AGENT_ENV, _REPORT_LABEL)


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
    run_daily_loop(CRON, TZ, _fire)


if __name__ == "__main__":
    _main_loop()
