"""Ship the local OTel trace mirror to the local Tempo viewer every 5 minutes.

Incremental (per-file watermark) so each run only sends new lines; idempotent
by span id, so a missed window is caught up on the next run. Best-effort:
failures are printed to the schedule log and retried on the next loop.
"""

import time
from datetime import UTC, datetime

import ava
from base.db import Database
from schedules.catchup import catch_up, fire_slot_once
from base.daemon.schedules.watcher import next_fire

CRON = "*/5 * * * *"
TIMEZONE = "UTC"


def ship_traces(_slot: datetime, _trigger: None) -> None:
    try:
        out = ava.shell.run("ava trace ship 2>&1 | tail -3", timeout=600)
        print(out)
    except Exception as exc:
        print(f"trace ship failed: {exc}")


def main() -> None:
    db = Database.from_settings()
    catch_up(db, [(CRON, None)], timezone=TIMEZONE, fire=ship_traces)
    while True:
        nxt = next_fire(CRON, after=datetime.now(UTC), timezone=TIMEZONE)
        while datetime.now(UTC) < nxt:
            time.sleep(30)
        fire_slot_once(db, nxt, None, fire=ship_traces)


if __name__ == "__main__":
    main()
