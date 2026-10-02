"""OS integration: the PlatformBackend facade and the OS jobs it registers.

``backend`` is the cross-platform facade; the job registrations it dispatches to
are ``autostart`` (boot-time autostart), ``boot_unit`` (the Linux systemd unit
that owns the application root), ``cron`` (the health-probe cron line),
``logs_job``,
``packages_job``, ``pr_flow_job`` and ``walg_job`` (recurring OS jobs), with
``boot_policy`` stating the boot retry policy once. ``probes`` holds the host
capability probes.
"""
