"""Ship the local OTel trace mirror to the local Tempo viewer every 5 minutes.

Incremental (per-file watermark) so each run only sends new lines; idempotent
by span id, so a missed window is caught up on the next run. Best-effort:
failures are printed to the schedule log and retried on the next loop.
"""

import time
from datetime import UTC, datetime
import ava
from shared.watcher import next_fire

while True:
    nxt = next_fire("*/5 * * * *", after=datetime.now(UTC), timezone="UTC")
    while datetime.now(UTC) < nxt:
        time.sleep(30)
    try:
        out = ava.shell.run("ava trace ship 2>&1 | tail -3", timeout=600)
        print(out)
    except Exception as e:
        print(f"trace ship failed: {e}")
