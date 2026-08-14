---
type: doc
title: Watchdog OS Monitoring — the watchdog itself is monitored
description: The watchdog is kept alive by an OS-level scheduled task (launchd / crontab / schtasks) running `ava cluster watchdog-probe` every 60s, reviving a dead pidfile; registered by converge, gated by AVA_OS_JOBS_ENABLED.
tags: []
---

# Watchdog OS Monitoring

## What it is
`shared/os_watchdog_probe.py` registers an OS-level scheduled task per capability — macOS launchd / Linux crontab / Windows schtasks — that runs `ava cluster watchdog-probe --role <role>` every 60s. If the watchdog's pidfile shows its process dead, the probe revives it.

Registration happens in the converge step (`cli/commands/_converge_os_jobs.py:ensure_watchdog_probe`) on every `ava start` / `ava update`, gated by `AVA_OS_JOBS_ENABLED` (default on). The `watchdog-probe` / `-register` / `-unregister` CLI commands live in `cli/parsers/cluster.py` (built by `cli/main.py`'s argparse tree).

## Key dependencies
- [[services/watchdog/watchdog.ava.okf.md]] — the watchdog this probe keeps alive

## Notes
- This is the answer to "who watches the watchdog" — the OS scheduler, not another Ava process.
