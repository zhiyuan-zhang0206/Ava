"""Converge steps that register this host's OS-level scheduled jobs.

Six jobs, one concept — everything Ava asks the platform scheduler (launchd /
crontab) to run on its behalf:

- **health probe** — periodic cluster health check reporting observations
  (gateway); it takes no repair action.
- **boot autostart** — brings the whole cluster back after a reboot (prod only).
- **logs maintenance** — daily copytruncate rotation followed by tiered retention.
- **packages refresh** — the content channel's recurring pass (skills fast lane).
- **PR flow** — the daily merge-pipeline sampler, credential-gated to the
  production home that can reach GitHub and Trunk (task #2139).
- **WAL-G tick** — the daily physical backup, present exactly while
  `AVA_WALG_CONFIG_FILE` is set.

They share a shape worth keeping together: each is idempotent, each delegates the
platform branching to a ``base.os_*`` module, and each fails the converge loudly
rather than leaving the cluster silently unsupervised.
"""

from __future__ import annotations

from cli.commands.converge.spec import ConvergeCtx


def ensure_health_probe_cron(ctx: ConvergeCtx) -> None:
    """Register the OS cron job for the cluster health probe.

    Only runs on gateway hosts (roles gated). Delegates to `base.host.system.cron`.
    The primary registration path is now in the gateway lifespan
    (`gateway/app.py`); this converge step is a belt-and-suspenders fallback
    that runs before the gateway process starts. Idempotent."""
    from base.host.system.cron import register_os_cron

    register_os_cron(enabled_reader=lambda: ctx.config.view.general.os_jobs_enabled)
    # On failure the exception propagates so converge fails fast.


def ensure_logs_maintenance(ctx: ConvergeCtx) -> None:
    """Register daily rotation followed by retention."""
    from base.host.system.logs_job import register_logs_job

    register_logs_job(enabled_reader=lambda: ctx.config.view.general.os_jobs_enabled)


def ensure_packages_refresh_job(ctx: ConvergeCtx) -> None:
    """Register the recurring content-refresh pass (design §5.6; task #3267).

    Every serving unit runs it: skills are per-machine state, so each home owns
    its own pass. Delegates to `base.host.system.packages_job`, which no-ops when
    `AVA_OS_JOBS_ENABLED` is off and skips registration when the refresh channel
    itself is disabled (`AVA_PACKAGES_REFRESH_ENABLED`); the registered command
    re-checks both at run time. Idempotent."""
    from base.host.system.packages_job import register_packages_job

    register_packages_job(
        enabled_reader=lambda: ctx.config.view.general.os_jobs_enabled,
        refresh_enabled_reader=lambda: ctx.config.view.packages.refresh_enabled,
        tick_reader=lambda: ctx.config.view.packages.refresh_tick_seconds,
    )
    # A registration failure propagates so converge fails fast.


def ensure_pr_flow_job(ctx: ConvergeCtx) -> None:
    """Register the daily PR-flow sampler job (task #2139).

    The gate lives in `base.host.system.pr_flow_job.register_pr_flow_job`: the job is
    registered only on a production home whose machine holds the sampler's
    credentials (`gh` on PATH + a Trunk API token) — in the fleet, macmini.
    Every other unit skips with the reason logged, so converge output explains
    the absence. Idempotent."""
    from base.host.system.pr_flow_job import register_pr_flow_job

    register_pr_flow_job(enabled_reader=lambda: ctx.config.view.general.os_jobs_enabled)


def ensure_walg_job(ctx: ConvergeCtx) -> None:
    """Keep the daily WAL-G tick registered exactly while WAL-G is switched on.

    Key set: register (idempotent). Key unset: remove a job a previous
    configuration left behind, so turning WAL-G off leaves no schedule that runs
    a command which now does nothing. Both directions only act in the default
    home (`owns_os_jobs`), and registration is a no-op where
    `AVA_OS_JOBS_ENABLED` is off. Delegates to `base.host.system.walg_job`."""
    from base.host.system.walg_job import register_walg_job, unregister_walg_job
    from services.backup.walg.config import enabled

    if enabled(path_reader=lambda: ctx.config.view.walg.walg_config_file):
        register_walg_job(
            enabled_reader=lambda: ctx.config.view.general.os_jobs_enabled,
            backup_hour_reader=lambda: ctx.config.view.services.backup_hour,
        )
    else:
        unregister_walg_job()


def ensure_cluster_autostart(ctx: ConvergeCtx) -> None:
    """Register the boot-time autostart job so a machine reboot brings this
    cluster's gateway / agents / daemons back up without a manual `ava start`
    (macOS launchd RunAtLoad / Linux systemd).

    host_global-gated to the prod install, so a dev worktree cluster never
    registers autostart (its plist would dangle once the worktree is removed).
    Delegates to `base.host.system.autostart`. Idempotent."""
    from base.host.system.autostart import register_autostart

    register_autostart(enabled_reader=lambda: ctx.config.view.general.os_jobs_enabled)
    # On failure the exception propagates so converge fails fast.
