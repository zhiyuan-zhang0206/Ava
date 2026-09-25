"""Converge steps that register this host's OS-level scheduled jobs.

Five jobs, one concept — everything Ava asks the platform scheduler (launchd /
crontab) to run on its behalf:

- **health probe** — periodic cluster health check with auto-rollback (gateway).
- **boot autostart** — brings the whole cluster back after a reboot (prod only).
- **logs maintenance** — daily copytruncate rotation followed by tiered retention.
- **packages refresh** — the content channel's recurring pass (skills fast lane).
- **PR flow** — the daily merge-pipeline sampler, credential-gated to the
  production home that can reach GitHub and Trunk (task #2139).

They share a shape worth keeping together: each is idempotent, each delegates the
platform branching to a ``shared.os_*`` module, and each fails the converge loudly
rather than leaving the cluster silently unsupervised — EXCEPT on Windows, where
a registration failure degrades to a loud warning instead (see
``WindowsPlatformBackend``): the failure class is transient (task #1196), and a
cluster that is down is worse than one that is up and loudly unsupervised.
"""

from __future__ import annotations

from cli.commands._converge_spec import ConvergeCtx


def ensure_health_probe_cron(_ctx: ConvergeCtx) -> None:
    """Register the OS cron job for the cluster health probe.

    Only runs on gateway hosts (roles gated). Delegates to `shared.os_cron`.
    The primary registration path is now in the gateway lifespan
    (`gateway/app.py`); this converge step is a belt-and-suspenders fallback
    that runs before the gateway process starts. Idempotent."""
    from shared.os_cron import register_os_cron

    register_os_cron()
    # On failure the exception propagates so converge fails fast on POSIX (the
    # cluster starts without a health probe, which is a degraded state). On
    # Windows the backend degrades to a warning instead — see
    # WindowsPlatformBackend.register_cron.


def ensure_logs_maintenance(_ctx: ConvergeCtx) -> None:
    """Register daily rotation followed by retention."""
    from shared.os_logs_job import register_logs_job

    register_logs_job()


def ensure_packages_refresh_job(_ctx: ConvergeCtx) -> None:
    """Register the recurring content-refresh pass (design §5.6; task #3267).

    Every serving unit runs it: skills are per-machine state, so each home owns
    its own pass. Delegates to `shared.os_packages`, which no-ops when
    `AVA_OS_JOBS_ENABLED` is off and skips registration when the refresh channel
    itself is disabled (`AVA_PACKAGES_REFRESH_ENABLED`); the registered command
    re-checks both at run time. Idempotent."""
    from shared.os_packages import register_packages_job

    register_packages_job()
    # POSIX: a registration failure propagates so converge fails fast (without
    # the job, content updates would silently stall until a manual refresh).
    # Windows degrades to a warning — see WindowsPlatformBackend.register_packages_job.


def ensure_pr_flow_job(_ctx: ConvergeCtx) -> None:
    """Register the daily PR-flow sampler job (task #2139).

    The gate lives in `shared.os_pr_flow.register_pr_flow_job`: the job is
    registered only on a production home whose machine holds the sampler's
    credentials (`gh` on PATH + a Trunk API token) — in the fleet, macmini.
    Every other unit skips with the reason logged, so converge output explains
    the absence. Idempotent."""
    from shared.os_pr_flow import register_pr_flow_job

    register_pr_flow_job()


def ensure_cluster_autostart(_ctx: ConvergeCtx) -> None:
    """Register the boot-time autostart job so a machine reboot brings this
    cluster's gateway / agents / daemons back up without a manual `ava start`
    (macOS launchd RunAtLoad / Linux @reboot crontab).

    host_global-gated to the prod install, so a dev worktree cluster never
    registers autostart (its plist would dangle once the worktree is removed).
    Delegates to `shared.os_autostart`. Idempotent."""
    from shared.os_autostart import register_autostart

    register_autostart()
    # On failure the exception propagates so converge fails fast on POSIX (the
    # cluster would silently not come back after a reboot otherwise). Windows
    # degrades to a warning — see WindowsPlatformBackend.register_autostart.
