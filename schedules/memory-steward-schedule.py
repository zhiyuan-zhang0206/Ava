# Memory Arbiter schedule — TEMPLATE (source of truth)
#
# This file is the version-controlled template for the cluster's memory
# schedule (gateway schedule id=2). The running copy lives at
# ~/.ava/schedules/2/memory-steward-schedule.py (deployed copy, same name) and is deployed with:
#
#     ava schedules update 2 --script-file <this file>
#     ava schedules restart 2
#
# Edit the template here, PR it, then deploy from the merged main.

"""Memory Arbiter schedule — 4AM consolidation, 7AM health check.

Gateway-hosted, resumable. Reuses the Memory Arbiter agent by label.
Fire times are CLUSTER wall clock (`AVA_TIMEZONE`, `cluster-pinned`), not the
host's OS timezone: every machine in the fleet fires at the same instant, and
moving a laptop across timezones does not move the schedule. Both hours sit in
the off-peak trough of a workday whose peak is 9-12 / 14-18 cluster time.
`AVA_TIMEZONE` is read at process start, so changing it needs
`ava schedules restart <id>`.
"""

import time
from datetime import UTC, datetime
import ava
from ava.agents import AgentStatus as S
from schedules.agent_status_guard import ensure_agent_status_members
from schedules.catchup import catch_up, fire_slot_once
from shared.config import settings
from shared.watcher import next_fire

ensure_agent_status_members(
    S,
    {"IDLING", "RESTARTING", "RUNNING", "TERMINATED"},
    schedule_name="memory-arbiter",
)

MEMORY_ARBITRATOR_LABEL = "memory-arbiter"
TIMEZONE = settings.general.timezone

TRIGGERS = [
    ("0 4 * * *", "Daily consolidation (commit + push + PR merge)"),
    ("0 7 * * *", "Daily health check — OKF, size, dedup, stale"),
    # 12PM merge removed (2026-08-01): merge/close/revert is the merge captain's job.
]


def ensure_agent(label, prompt):
    """Find or resurrect the Memory Steward agent by label."""
    mine = [
        a
        for a in ava.agents.list_agents(
            filter_by_status=(
                S.RUNNING,
                S.IDLING,
                S.TERMINATED,
                S.RESTARTING,
            )
        )
        if a.label == label
    ]
    if mine:
        a = max(mine, key=lambda r: r.agent_id)
        if a.status == S.TERMINATED:
            ava.agents.resurrect(a.agent_id, prompt)
        else:
            ava.agents.send_message(a.agent_id, prompt)
        return a.agent_id
    return ava.agents.spawn(prompt=prompt, label=label)  # pyright: ignore[reportCallIssue] — fleet plugin wraps spawn with label


def fire_memory_maintenance(message: str) -> None:
    print(f"[schedule] Firing: {message}")
    try:
        full_prompt = f"memory-arbiter: {message}"
        ensure_agent(MEMORY_ARBITRATOR_LABEL, full_prompt)
        print(f"[schedule] Agent notified: {message}")
    except Exception as exc:
        print(f"[schedule] Failed: {exc}")


def main():
    catch_up(TRIGGERS, timezone=TIMEZONE, fire=fire_memory_maintenance)
    while True:
        # Find the next fire among all triggers
        now = datetime.now(UTC)
        next_times = []
        for cron_expr, message in TRIGGERS:
            nxt = next_fire(cron_expr, after=now, timezone=TIMEZONE)
            next_times.append((nxt, cron_expr, message))

        # Pick the earliest
        next_fire_time, cron_expr, message = min(next_times, key=lambda x: x[0])

        # Sleep until then
        wait = (next_fire_time - datetime.now(UTC)).total_seconds()
        if wait > 0:
            print(f"[schedule] Next: {next_fire_time} ({cron_expr}) — sleeping {wait:.0f}s")
            time.sleep(wait)

        fire_slot_once(next_fire_time, message, fire=fire_memory_maintenance)

        # Small debounce to avoid double-fire
        time.sleep(60)


if __name__ == "__main__":
    main()
