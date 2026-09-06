"""Daily CI-minute reconciliation — the C9 daily report host.

Fires once per day at 05:00 cluster time (after the 4AM memory consolidation),
collects the trailing 24h of CI runs, appends them to the attribution ledger
idempotently (keyed by run id — `scripts/ci_accounting.py`), and emits one
`ci_usage_daily` telemetry event carrying the day's totals. The per-agent
breakdown stays in the ledger; `ci_utils.py --ci-usage` reads it on demand.

Idempotency: the slot claim (`schedules.catchup.fire_slot_once`) is
at-most-once, and the ledger append is run-id-keyed, so a re-run of the same
window is a no-op for already-recorded runs. A failed reconciliation does NOT
retry automatically (the claim stays committed — the documented
at-most-once trade-off); the failure message tells the P0 lead to backfill
manually with `scripts/ci_accounting.py --since ... --until ... --append-ledger`.
"""

from __future__ import annotations

import os
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import ava
from ava.agents import AgentStatus as S
from schedules.agent_status_guard import ensure_agent_status_members
from schedules.catchup import catch_up, fire_slot_once
from shared.config import settings
from shared.log import init_gateway_process
from shared.watcher import next_fire, previous_fire

ensure_agent_status_members(
    S,
    {"IDLING", "RUNNING", "TERMINATED"},
    schedule_name="c9-daily-report",
)

CRON = "0 5 * * *"
# The cluster default is Asia/Shanghai. Config-derived like the other
# built-ins: one cluster wall clock even when operators change it.
TZ = settings.general.timezone
_REPORT_AGENT_ENV = "AVA_CI_USAGE_REPORT_AGENT"
_REPORT_LABEL = "Ava \u8d1f\u8d23\u4eba"
_REPO_ROOT = Path(__file__).resolve().parents[1]
_PROCESS_NAME = "schedule-c9-daily"

# GitHub-hosted overage rates (private-repo equivalent; scripts/ci_accounting.py).
_LINUX_MINUTE_USD = 0.006
_MACOS_MINUTE_USD = 0.062


def _report_agent() -> int:
    configured = os.environ.get(_REPORT_AGENT_ENV)
    if configured is not None and configured.strip():
        try:
            agent_id = int(configured)
        except ValueError as exc:
            raise RuntimeError(f"{_REPORT_AGENT_ENV} must be a numeric agent id") from exc
        if agent_id <= 0:
            raise RuntimeError(f"{_REPORT_AGENT_ENV} must be a positive agent id")
        return agent_id
    matches = [
        agent
        for agent in ava.agents.list_agents(filter_by_status=(S.RUNNING, S.IDLING, S.TERMINATED))
        if agent.label == _REPORT_LABEL
    ]
    if not matches:
        raise RuntimeError(f"no report agent labelled {_REPORT_LABEL!r} is available")
    return max(matches, key=lambda agent: agent.agent_id).agent_id


def _report_failure(detail: str) -> None:
    message = (
        f"C9 daily reconciliation failed:\n{detail[-1000:]}\n"
        "Check the schedule log; backfill the missed window manually with "
        "`scripts/ci_accounting.py --since ... --until ... --append-ledger`."
    )
    try:
        ava.agents.send_message(_report_agent(), message)
    except Exception as exc:
        print(f"c9-daily-report could not notify the P0 lead: {exc}")
        raise


def window_bounds(slot_end: datetime) -> tuple[str, str, str]:
    """The reconciliation window for a slot: [slot_end - 24h, slot_end).

    Slot-based (not trailing-now), so a late catch-up fire still covers its
    own 05:00-05:00 day and windows stay gapless across fires. Returns the
    UTC ISO since/until pair plus the window's day label (cluster-tz date).
    """
    since = slot_end - timedelta(hours=24)
    day = slot_end.astimezone(ZoneInfo(TZ))
    return (
        since.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        slot_end.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        day.strftime("%Y-%m-%d"),
    )


def _load_accounting() -> Any:
    """Import `scripts/ci_accounting.py` (scripts/ is not a package)."""
    scripts_dir = _REPO_ROOT / "scripts"
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    return __import__("ci_accounting")


def summarize(
    entries: list[dict],
    *,
    day: str,
    window_start: str,
    window_end: str,
    appended_runs: int,
) -> dict:
    """Flat `ci_usage_daily` payload from the window's attribution entries."""
    attributed = [e for e in entries if isinstance(e.get("agent_id"), int)]
    linux_minutes = sum(int(e.get("linux_minutes") or 0) for e in entries)
    macos_minutes = sum(int(e.get("macos_minutes") or 0) for e in entries)
    attributed_minutes = sum(
        int(e.get("linux_minutes") or 0) + int(e.get("macos_minutes") or 0) for e in attributed
    )
    return {
        "day": day,
        "window_start": window_start,
        "window_end": window_end,
        "runs": len(entries),
        "attributed_runs": len(attributed),
        "unattributed_runs": len(entries) - len(attributed),
        "total_minutes": linux_minutes + macos_minutes,
        "attributed_minutes": attributed_minutes,
        "linux_minutes": linux_minutes,
        "macos_minutes": macos_minutes,
        "appended_runs": appended_runs,
        "est_usd": round(
            linux_minutes * _LINUX_MINUTE_USD + macos_minutes * _MACOS_MINUTE_USD,
            2,
        ),
    }


def _fire(_payload: None) -> None:
    try:
        accounting = _load_accounting()
        slot_end = previous_fire(CRON, before=datetime.now(UTC), timezone=TZ)
        since, until, day = window_bounds(slot_end)
        entries = accounting.collect(accounting.DEFAULT_REPO, since, until)
        appended = accounting.append_ledger(accounting.DEFAULT_LEDGER, entries)
        init_gateway_process(name=_PROCESS_NAME)
        from shared import telemetry

        telemetry.emit(
            "telemetry",
            "ci_usage_daily",
            source="system",
            attributes=summarize(
                entries,
                day=day,
                window_start=since,
                window_end=until,
                appended_runs=appended,
            ),
        )
        print(
            f"[{datetime.now(UTC).isoformat()}] c9-daily-report: {day} — "
            f"{len(entries)} runs, {appended} appended"
        )
    except Exception as exc:
        print(f"c9-daily-report failed: {exc}")
        _report_failure(f"{type(exc).__name__}: {exc}")


def _main_loop() -> None:
    catch_up([(CRON, None)], timezone=TZ, fire=_fire)
    last_run_at = datetime.now(UTC)
    while True:
        now = datetime.now(UTC)
        next_run = next_fire(CRON, after=now - timedelta(minutes=2), timezone=TZ)
        if next_run <= last_run_at:
            next_run = next_fire(CRON, after=last_run_at, timezone=TZ)
        wait_seconds = (next_run - now).total_seconds()
        if wait_seconds > 0:
            time.sleep(min(wait_seconds, 3600))
            continue
        fire_slot_once(next_run, None, fire=_fire)
        last_run_at = datetime.now(UTC)
        time.sleep(120)


if __name__ == "__main__":
    _main_loop()
