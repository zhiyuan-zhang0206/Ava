from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

_CRON_MARKERS = (
    "# ava-health-probe",
    "# ava-autostart",
    "# ava-logs-maintenance",
    "# ava-packages-refresh",
    "# ava-pr-flow",
    "# ava-walg",
)


def _launchd_dir() -> Path:
    return Path.home() / "Library" / "LaunchAgents"


def _launchd_jobs() -> set[str]:
    agents = _launchd_dir()
    if not agents.is_dir():
        return set()
    return {
        f"launchd:{p.name}@{hashlib.sha256(p.read_bytes()).hexdigest()[:12]}"
        for p in agents.glob("com.ava.*.plist")
    }


def crontab_jobs() -> set[str]:
    try:
        res = subprocess.run(["crontab", "-l"], capture_output=True, text=True, check=False)
    except OSError:
        return set()
    if res.returncode != 0:
        return set()
    return {
        f"crontab:{line.strip()}"
        for line in res.stdout.splitlines()
        if any(m in line for m in _CRON_MARKERS)
    }


def host_ava_os_jobs() -> frozenset[str]:
    return frozenset(_launchd_jobs() | crontab_jobs())
