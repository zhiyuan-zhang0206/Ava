"""Converge steps that register this host's OS-level scheduled jobs.

Five jobs, one concept — everything Ava asks the platform scheduler (launchd /
crontab) to run on its behalf:

- **health probe** — periodic cluster health check with auto-rollback (gateway).
- **watchdog probe** — revives a dead per-capability watchdog (any serving role); retired instead on a root-driven host (the root supervisor absorbs the watchdogs).
- **boot autostart** — brings the whole cluster back after a reboot (prod only).
- **logs maintenance** — daily copytruncate rotation followed by tiered retention.
- **packages refresh** — the content channel's recurring pass (skills fast lane).

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


def ensure_watchdog_probe(ctx: ConvergeCtx) -> None:
    """Keep the OS-scheduled watchdog probe in step with this host's service driver.

    Session mode (the default): register ONE job per capability this unit
    carries, not one per host — the watchdog daemons are per-capability (a
    single box runs both `ava-gateway-watchdog` and `ava-agent-runner-watchdog`),
    so a single probe would leave the other capability's watchdog unsupervised —
    the same collision that motivated splitting the watchdog itself.

    Root mode (`services.root_driver_enabled` on for this host — the same rule
    `ava start`/`ava stop` fork on): RETIRE the probe instead. The root
    supervisor's own HealthMonitor absorbs the watchdogs (the unit manifests
    drop `ABSORBED_WATCHDOGS`), so a probe-revived legacy watchdog would run a
    second supervision path beside the root tree. Retirement is idempotent, and
    the next converge with the switch back off falls through to the register
    branch — the gray-rollout revert restores legacy supervision by itself.
    Deliberately NOT gated on `os_jobs_enabled()`: cleanup has to work wherever
    registration is forbidden too, the same rule `shared.os_cron` states for
    deregistration.

    `ctx.roles` is the unit's capability SET and is `frozenset[str]` off the DB,
    so it is filtered through the known capabilities rather than trusted: a
    gateway-only host carries one job, an agent-runner-only host one, a single
    box two, and an unknown token none. Delegates to `shared.os_watchdog_probe`;
    idempotent either way."""
    import cli.commands as _ns

    carried = ctx.roles or frozenset()
    if _ns._root_driven_enabled():
        from shared.os_watchdog_probe import unregister_watchdog_probe

        for role in CAPABILITY_ORDER:
            if role in carried:
                unregister_watchdog_probe(role)
        return
    from shared.os_watchdog_probe import register_watchdog_probe

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
