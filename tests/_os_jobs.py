"""Host OS-scheduled job inventory — the eyes of the session-level leak guard.

`tests/conftest.py` snapshots this at import and re-reads it in
`pytest_sessionfinish`: anything that appeared in between is a job some test
handed to the platform scheduler and left running on the developer's machine.

That is not hypothetical. Before `AVA_OS_JOBS_ENABLED` existed, the e2e gateway
subprocess ran the real lifespan, which registers the health probe — nine
`com.ava.ava_e2e_home_<pid>_<ns>-<hash>.health-probe` LaunchAgents accumulated on
one dev box, each firing every 300s with `--auto-rollback` long after the run
(and the worktree they pointed at) was gone. The pytest-process monkeypatch that
was supposed to prevent it could not reach a subprocess.

Reads the host's REAL namespace on purpose — `Path.home()` here is the
operator's home, not a test-scoped one, because the leak this guard exists to
catch is precisely a write that escaped every test-scoped redirect.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from shared.platform import IS_WINDOWS

# The crontab comment markers the four registrars stamp their lines with
# (`shared.os_cron` / `os_autostart` / `os_watchdog_probe` / `os_logs_job`). A line carrying one
# is an Ava job; anything else in the user's crontab is theirs and is ignored.
_CRON_MARKERS = (
    "# ava-health-probe",
    "# ava-autostart",
    "# ava-watchdog-probe",
    "# ava-logs-maintenance",
)


def _launchd_dir() -> Path:
    """Resolved per call, not cached at import: the leak-guard reads the real
    home, but the unit tests point HOME at a tmp dir to plant a fake job."""
    return Path.home() / "Library" / "LaunchAgents"


def _launchd_jobs() -> set[str]:
    agents = _launchd_dir()
    if not agents.is_dir():
        return set()
    return {f"launchd:{p.name}" for p in agents.glob("com.ava.*.plist")}


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


def _schtasks_jobs() -> set[str]:
    """Task Scheduler entries under the `\\Ava\\` folder this repo owns."""
    if not IS_WINDOWS:
        return set()
    try:
        res = subprocess.run(
            ["schtasks", "/Query", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return set()
    if res.returncode != 0:
        return set()
    names: set[str] = set()
    for line in res.stdout.splitlines():
        name = line.split(",")[0].strip('"') if line else ""
        if name.startswith("\\Ava\\"):
            names.add(f"schtasks:{name}")
    return names


def host_ava_os_jobs() -> frozenset[str]:
    """Every OS-scheduled job on this host that belongs to Ava, as opaque ids.

    The id doubles as the removal instruction (`remove_os_job` parses it back),
    so the guard can report AND undo a leak without re-deriving which cluster the
    job claimed to belong to.
    """
    return frozenset(_launchd_jobs() | _crontab_jobs() | _schtasks_jobs())


# Basenames of the two throwaway homes a pytest session creates — the tmpfs home
# in `tests/conftest.py` and the per-session e2e home in `tests/e2e/conftest.py`.
# Every job id carries the home slug (launchd label / schtasks folder / the
# crontab line's marker), so a substring test identifies a job this suite owns.
_TEST_HOME_PREFIXES = ("ava_test_home_", "ava_e2e_home_")


def is_test_owned_job(job: str) -> bool:
    """True when `job`'s id names one of this suite's throwaway homes.

    The guard fails the run on ANY job that appeared during the session, but only
    deletes the ones this answers True for. A developer who runs `ava start` in
    another terminal mid-suite registers real prod jobs on the same host; a guard
    that swept "anything new" would deregister them — the same
    delete-the-wrong-cluster's-job failure this whole change is about.
    """
    return any(p in job for p in _TEST_HOME_PREFIXES)


def remove_os_job(job: str) -> None:
    """Undo one id returned by `host_ava_os_jobs`. Best-effort and idempotent.

    The guard removes what leaked rather than only reporting it: nine plists
    accumulated on one box precisely because every individual run only ever
    reported (to nobody). Deliberately not routed through
    `shared.os_*.unregister_*` — those address a job by the cluster that owns it,
    and a leaked job's cluster is by definition gone.
    """
    kind, _, value = job.partition(":")
    if kind == "launchd":
        label = value.removesuffix(".plist")
        subprocess.run(  # noqa: S603 — label came from this host's own LaunchAgents dir
            ["launchctl", "bootout", f"gui/{os.getuid()}/{label}"],
            capture_output=True,
            check=False,
        )
        (_launchd_dir() / value).unlink(missing_ok=True)
    elif kind == "crontab":
        res = subprocess.run(["crontab", "-l"], capture_output=True, text=True, check=False)
        if res.returncode != 0:
            return
        kept = [line for line in res.stdout.splitlines() if line.strip() != value]
        subprocess.run(
            ["crontab", "-"], input="\n".join(kept) + "\n", capture_output=True, check=False
        )
    elif kind == "schtasks":
        subprocess.run(  # noqa: S603 — name came from this host's own schtasks query
            ["schtasks", "/Delete", "/TN", value, "/F"], capture_output=True, check=False
        )
