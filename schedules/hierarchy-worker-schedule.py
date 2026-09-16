# Hierarchy worker — the compact-driven understanding-tree builder (task #3704 P2b).
#
# A resident schedule (no cron slot): the loop scans for new compaction
# boundaries, enqueues per-agent build jobs, and runs them serially, one
# child process per job. All supervision (launch at boot, adopt across
# gateway restarts, crash backoff + breaker, run history) is the
# ScheduleManager's; this file is the thin host — the loop itself lives in
# services/hierarchy_worker/runner.py so it is importable and testable.
#
# Deploy: built-in manifest entry `hierarchy-worker` (class product, enabled).
# Script changes roll out like every built-in:
#   ava schedules update <id> --script-file schedules/hierarchy-worker-schedule.py
#   ava schedules restart <id>

"""Hierarchy worker: scan compact boundaries, build understanding trees."""

from services.hierarchy_worker.runner import loop_forever

if __name__ == "__main__":
    loop_forever()
