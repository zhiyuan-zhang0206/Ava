"""Converge steps that register this host's OS-level scheduled jobs.

Four jobs, one concept — everything Ava asks the platform scheduler (launchd /
crontab) to run on its behalf:

- **health probe** — periodic cluster health check with auto-rollback (gateway).
- **watchdog probe** — revives a dead per-capability watchdog (any serving role).
- **boot autostart** — brings the whole cluster back after a reboot (prod only).
- **logs maintenance** — daily copytruncate rotation followed by tiered retention.

They share a shape worth keeping together: each is idempotent, each delegates the
platform branching to a ``shared.os_*`` module, and each fails the converge loudly
rather than leaving the cluster silently unsupervised — EXCEPT on Windows, where
a registration failure degrades to a loud warning instead (see
``WindowsPlatformBackend``): the failure class is transient (task #1196), and a
cluster that is down is worse than one that is up and loudly unsupervised.

Plus the Windows-only **reap** step: stale-slug tasks under ``\\Ava\\`` (the
ghost-task class behind task #1196) are deleted before the register steps run.
"""

from __future__ import annotations

from cli.commands._converge_spec import CAPABILITY_ORDER, ConvergeCtx


def reap_stale_schtasks(_ctx: ConvergeCtx) -> None:
    """Delete Task Scheduler jobs under ``\\Ava\\`` left behind by a home-slug
    change (the win 2026-08-11 ghost-task class: old-slug tasks keep firing and
    race the current slug's `/Create` on every converge). Runs before the
    register steps below, so a host that once carried an older slug converges
    clean. A no-op on POSIX (no Task Scheduler).

    Never fails converge: reap is best-effort cleanup, and the register steps
    that follow (re-)arm the current tasks regardless.
    """
    from shared.os_schtasks import reap_stale_tasks

    reap_stale_tasks()


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
    """Register daily rotation + retention and reap the old manual macOS job."""
    from shared.os_logs_job import (
        reap_legacy_logs_job,
        register_logs_job,
    )

    register_logs_job()
    reap_legacy_logs_job()


def ensure_watchdog_probe(ctx: ConvergeCtx) -> None:
    """Register the OS-scheduled probe that revives a dead watchdog.

    ONE job per capability this unit carries, not one per host: the watchdog
    daemons are per-capability (a single box runs both `ava-gateway-watchdog`
    and `ava-agent-runner-watchdog`), so a single probe would leave the other
    capability's watchdog unsupervised — the same collision that motivated
    splitting the watchdog itself.

    `ctx.roles` is the unit's capability SET and is `frozenset[str]` off the DB,
    so it is filtered through the known capabilities rather than trusted: a
    gateway-only host registers one job, an agent-runner-only host registers one,
    a single box registers two, and an unknown token registers nothing. Delegates
    to `shared.os_watchdog_probe`; idempotent."""
    from shared.os_watchdog_probe import register_watchdog_probe

    carried = ctx.roles or frozenset()
    for role in CAPABILITY_ORDER:
        if role in carried:
            # POSIX: failure propagates so converge fails fast (a dead watchdog
            # would not be revived). Windows degrades to a warning — see
            # WindowsPlatformBackend.register_watchdog_probe.
            register_watchdog_probe(role)


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
