"""Host OS-scheduled job inventory — the eyes of the session-level leak guard.

`tests/fixtures/provisioning.py` snapshots this at import and re-reads it in
`pytest_sessionfinish`: anything that appeared or changed in between is a job some
test handed to the platform scheduler on the developer's machine.

That is not hypothetical. Before `AVA_OS_JOBS_ENABLED` existed, the e2e gateway
subprocess ran the real lifespan, which registers the health probe — nine
health-probe LaunchAgents accumulated on one dev box, each firing every 300s with
`--auto-rollback` long after the run (and the worktree they pointed at) was gone.
The pytest-process monkeypatch that was supposed to prevent it could not reach a
subprocess.

Labels and crontab markers name a job, not a home, so a leak now REPLACES the
host's real job instead of adding a second one. The inventory therefore carries a
digest of each plist: a rewritten job shows up as new. The guard only reports.
Nothing is swept, because a job it found is indistinguishable from the real one.

Reads the host's REAL namespace on purpose — `Path.home()` here is the
operator's home, not a test-scoped one, because the leak this guard exists to
catch is precisely a write that escaped every test-scoped redirect.
"""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

# The crontab comment markers the registrars stamp their lines with
# (`base.host.system.cron` / `autostart` / `logs_job` / `packages_job` / `pr_flow_job` /
# `walg_job`).
# A line carrying one is an Ava job; anything else in the user's crontab is theirs
# and is ignored.
_CRON_MARKERS = (
    "# ava-health-probe",
    "# ava-autostart",
    "# ava-logs-maintenance",
    "# ava-packages-refresh",
    "# ava-pr-flow",
    "# ava-walg",
)


def _launchd_dir() -> Path:
    """Resolved per call, not cached at import: the leak-guard reads the real
    home, but the unit tests point HOME at a tmp dir to plant a fake job."""
    return Path.home() / "Library" / "LaunchAgents"


def _launchd_jobs() -> set[str]:
    agents = _launchd_dir()
    if not agents.is_dir():
        return set()
    return {
        f"launchd:{p.name}@{hashlib.sha256(p.read_bytes()).hexdigest()[:12]}"
        for p in agents.glob("com.ava.*.plist")
    }


def _crontab_jobs() -> set[str]:
    """Ava-marked lines in the user's crontab. A host with no `crontab` binary
    (macOS ships one, a hermetic container may not) contributes none."""
    try:
        res = subprocess.run(["crontab", "-l"], capture_output=True, text=True, check=False)
    except OSError:
        return set()
    if res.returncode != 0:
        return set()  # "no crontab for <user>" — nothing registered
    return {
        f"crontab:{line.strip()}"
        for line in res.stdout.splitlines()
        if any(m in line for m in _CRON_MARKERS)
    }


def host_ava_os_jobs() -> frozenset[str]:
    """Every OS-scheduled job on this host that belongs to Ava, as opaque ids that
    change when the job's definition does."""
    return frozenset(_launchd_jobs() | _crontab_jobs())
