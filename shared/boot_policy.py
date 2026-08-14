"""The boot-autostart retry policy — one behaviour, stated once.

At boot, the things `ava start` needs are not all up yet. The incident that
produced this module: an enrolled agent-runner fetches `GET /api/bootstrap`
from the gateway as part of its Settings build (`shared.bootstrap`; every
runner process does this at startup), the VPN interface came up *after* the
boot job fired, the fetch raised ENETUNREACH, `ava start` exited 1 — and
nothing ever tried again, so the runner stayed down for hours. Any transient
boot-time dependency (DNS, a not-yet-mounted volume, a gateway still starting)
has that same shape.

The policy: **re-run `ava start` every ``BOOT_RETRY_INTERVAL_S`` seconds until
it exits 0, with no attempt limit.** Identical on all three platforms; only the
mechanism differs, because only one of the three schedulers can retry a job on
our behalf:

- **macOS** — launchd does the retrying (`KeepAlive` → `SuccessfulExit: false`,
  `ThrottleInterval` = the interval). Preferred where it exists: launchd is a
  supervisor, so it also restarts a start that *died* rather than exited, which
  a loop living inside that process could not.
- **Linux / Windows** — neither scheduler can repeat a boot trigger: a cron
  `@reboot` line fires exactly once, and `schtasks /RI` is documented as "not
  applicable for schedule types: MINUTE, HOURLY, ONSTART, ONLOGON, ONIDLE, and
  ONEVENT". So the loop is ours — `cli.boot_retry` (`ava boot`).

**Why no attempt cap.** An earlier draft capped the owned loop at ~30 attempts,
on the theory that the 60 s OS watchdog probe was the real long-run net and
`ava boot` only had to cover the first few minutes. That theory is false, and a
cap would have reintroduced this same outage with a longer fuse: a box that
boots while its VPN is down for 45 minutes would recover on macOS and stay down
forever on Linux/Windows. The probe
(`cli/commands/_cluster_watchdog_probe.py`) repairs exactly one thing — a dead
watchdog session — and says so: "It does NOT run `ava start`, and it does
not touch any other service." Neither it nor the watchdog it revives ever runs
the gateway env refresh, the converge phase, or this cluster's pg/redis
bring-up (`ensure_cluster_instance` has no caller outside `cli/`). So on a host
whose boot `ava start` never succeeded, the probe cannot finish the job and the
retry has to keep going. It costs one child process per minute, and the first
success ends it.

**What the retry may see.** The policy is safe only because the codes it retries
are codes a retry can fix. `ava start` also exits
`SERVICES_NOT_READY_EXIT_CODE` when every step succeeded but a launched service
never passed its liveness probe, and an unbounded retry on *that* is the outage
this module's own reasoning forbids in the other direction: a box whose headed
Chrome will never launch would re-run `ava start` every 60 s forever while
otherwise serving perfectly. So the boot path — the owned loop AND the launchd
plist, since `SuccessfulExit` is a boolean and cannot distinguish codes — runs
start with `--no-readiness-gate`, and the retried set stays "a step failed"
(1). Reviving a service that launched and then died is the watchdog keepalive's
job, and that watchdog is one of the services the start just launched.

Deliberately import-free: `cli.boot_retry` is dispatched by `cli.main` before
the settings-gated `cli.commands` import (the `ava enroll` slot), so anything it
reaches must not build `Settings()`.
"""

from __future__ import annotations

# Seconds between two `ava start` attempts — launchd's ThrottleInterval on
# macOS, the sleep between iterations of the owned loop elsewhere. There is
# deliberately no companion attempt cap; see the module docstring.
BOOT_RETRY_INTERVAL_S = 60
