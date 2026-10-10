"""OS cron registration for the cluster health probe — `ava cluster health-probe-register`
and `ava cluster health-probe-unregister`.

Thin CLI wrappers that delegate to `base.host.system.cron`. The core logic lives in the
base layer so both the gateway lifespan (primary registration path) and the
CLI converge step (belt-and-suspenders fallback) can call the same functions
without violating the import layering.

Called by `ava start` on bring-up (via `cmd_cron_register`) so the cron job
is always present when the cluster is running.
"""

from __future__ import annotations

from base.config import ConfigBoot
from base.host.system.cron import (
    DEFAULT_INTERVAL_SECONDS,
    register_os_cron,
    unregister_os_cron,
)


def cmd_cron_register(
    *,
    interval_s: int = DEFAULT_INTERVAL_SECONDS,
) -> int:
    """Register the OS cron job for the cluster health probe.

    CLI entry — delegates to `base.host.system.cron.register_os_cron`. Idempotent —
    re-running updates the interval and reloads the job."""
    config = ConfigBoot()

    def enabled_reader() -> bool:
        if not config.prepared:
            config.read_process_environment()
        return config.view.general.os_jobs_enabled

    try:
        register_os_cron(interval_s=interval_s, enabled_reader=enabled_reader)
    except RuntimeError as e:
        print(f"  * {e}")
        return 1
    return 0


def cmd_cron_unregister() -> int:
    """Remove the OS cron job for the cluster health probe.

    CLI entry — delegates to `base.host.system.cron.unregister_os_cron`."""
    try:
        unregister_os_cron()
    except RuntimeError as e:
        print(f"  * {e}")
        return 1
    return 0
