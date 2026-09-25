# Hierarchy worker host — a built-in schedule consuming one cron slot a minute
# (task #3704 P2b). Each slot runs one tick: claim and run due jobs
# back-to-back in child processes, draining what the event trigger enqueued
# (each compact boundary lands its own job, task #4674; a clean budget
# continuation drains immediately). A low-frequency reconcile scan runs behind
# that trigger as the safety net for lost events and stranded retries. The
# worker's durable state is the job layer in the DB (partial unique index +
# atomic claim + stale recovery), so a missed slot loses nothing — the slot
# paces the idle wait and keeps the worker in the fleet's standard schedule
# ledger.
#
# The tick body lives in services/hierarchy_worker/runner.py so it is
# importable and testable; this file is the thin host, using the same slot
# loop as the other built-ins (trace-ship-tempo shape).
#
# Deploy: built-in manifest entry `hierarchy-worker` (class product, enabled).
# Script changes roll out like every built-in:
#   ava schedules update <id> --script-file schedules/hierarchy-worker-schedule.py
#   ava schedules restart <id>

"""Hierarchy worker: per-minute tick — drain build jobs (event-fed, scan-backed)."""

import time
from datetime import UTC, datetime

from schedules.catchup import catch_up, fire_slot_once
from services.hierarchy_worker.runner import prepare, run_tick
from shared.config import settings
from shared.daemon.schedules.watcher import next_fire

# One tick a minute: the scan is one aggregated query over `checkpoints`, so
# a compact reaches its build within a minute; while jobs are pending the
# tick drains them back-to-back, so the cadence paces only the idle wait.
CRON = "* * * * *"
TZ = settings.general.timezone


def _fire_tick(_trigger: None) -> None:
    run_tick()


def main() -> None:
    prepare()
    catch_up([(CRON, None)], timezone=TZ, fire=_fire_tick)
    while True:
        nxt = next_fire(CRON, after=datetime.now(UTC), timezone=TZ)
        while datetime.now(UTC) < nxt:
            time.sleep(30)
        fire_slot_once(nxt, None, fire=_fire_tick)


if __name__ == "__main__":
    main()
