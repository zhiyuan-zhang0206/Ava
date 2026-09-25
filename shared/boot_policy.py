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
it exits 0, with no attempt limit.** Identical on every platform; only the
mechanism differs, because only some schedulers can retry a job on our behalf:

- **macOS** — launchd does the retrying (`KeepAlive` → `SuccessfulExit: false`,
  `ThrottleInterval` = the interval). Preferred where it exists: launchd is a
  supervisor, so it also restarts a start that *died* rather than exited, which
  a loop living inside that process could not.
- **Linux with systemd** — the distro-level boot unit (`shared.os_boot_unit`,
  installed with `ava cluster boot-unit install`) states the same policy in
  restart keys: `Restart=on-failure`, `RestartSec` = the interval,
  `StartLimitIntervalSec=0` for the no-cap half. `TimeoutStartSec` bounds the
  initial readiness attempt. On success, the ordinary start tail transfers
  MainPID to the verified application root with an acknowledged notification;
  systemd then owns root lifetime without a resident wrapper or runtime cap.
- **Linux without the unit / Windows** — these schedulers cannot repeat a boot
  trigger: a cron `@reboot` line fires exactly once, and `schtasks /RI` is
  documented as "not applicable for schedule types: MINUTE, HOURLY, ONSTART,
  ONLOGON, ONIDLE, and ONEVENT". So the loop is ours — `cli.boot_retry`
  (`ava boot`).

**Why no attempt cap.** Dependencies can recover after an arbitrary outage.
The first successful start ends the boot attempt; later service health belongs
to ava-root. Repeating start reconciles persisted identity and owned services;
it must not replace a healthy generation just because another unit is unready.

**Readiness is required.** Every boot mechanism runs the ordinary start entry.
Unready critical services keep a nonzero result and remain visible in the boot
log. Root owns service revival, so retries do not create another service owner
or claim readiness before the application's probes pass.

Deliberately import-free: `cli.boot_retry` is dispatched by `cli.main` before
the settings-gated `cli.commands` import (the first-start dispatch slot), so anything it
reaches must not build `Settings()`.
"""

from __future__ import annotations

# Seconds between two `ava start` attempts — launchd's ThrottleInterval on
# macOS, the sleep between iterations of the owned loop elsewhere. There is
# deliberately no companion attempt cap; see the module docstring.
BOOT_RETRY_INTERVAL_S = 60
