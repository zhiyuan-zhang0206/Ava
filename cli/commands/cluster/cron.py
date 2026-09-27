"""OS cron registration for the cluster health probe — `ava cluster health-probe-register`
and `ava cluster health-probe-unregister`.

Thin CLI wrappers that delegate to `shared.os_cron`. The core logic lives in the
shared layer so both the gateway lifespan (primary registration path) and the
CLI converge step (belt-and-suspenders fallback) can call the same functions
without violating the import layering.

Called by `ava start` on bring-up (via `cmd_cron_register`) so the cron job
is always present when the cluster is running.
"""

from __future__ import annotations

from shared.os_cron import (
    DEFAULT_CONSECUTIVE_THRESHOLD,
    DEFAULT_INTERVAL_SECONDS,
    register_os_cron,
    unregister_os_cron,
)


def cmd_cron_register(
    *,
    interval_s: int = DEFAULT_INTERVAL_SECONDS,
    threshold: int = DEFAULT_CONSECUTIVE_THRESHOLD,
) -> int:
    """Register the OS cron job for the cluster health probe.

    CLI entry — delegates to `shared.os_cron.register_os_cron`. Idempotent —
    re-running updates the interval and reloads the job."""
    try:
        register_os_cron(interval_s=interval_s, threshold=threshold)
    except RuntimeError as e:
        print(f"  * {e}")
        return 1
    return 0


def cmd_cron_unregister() -> int:
    """Remove the OS cron job for the cluster health probe.

    CLI entry — delegates to `shared.os_cron.unregister_os_cron`."""
    try:
        unregister_os_cron()
    except RuntimeError as e:
        print(f"  * {e}")
        return 1
    return 0
