"""The launchd job surface of the macOS permissions helper.

One home for the per-cluster LaunchAgent identity — label, plist path, launchd
domain — and for bounded ``launchctl print`` inspection of the job. The
build/cert/repair steps stay in ``lifecycle``; that module delegates the job
identity here so the label formula exists once, and the helper healthcheck
(task #3393) reads and parses the job here so the ``launchctl print`` field
vocabulary exists once too.

The parse reads the ``job state`` line deliberately: a stuck job's top-level
``state`` still reads ``spawn scheduled``/``xpcproxy`` — ``spawn failed`` lives
only in ``job state`` (F5 findings section 6; PR #2500 review). Reads are
bounded and total: a hung tool or an absent job folds into a sentinel result,
never an exception — the callers are healthchecks whose whole point is to
answer even when the system under them does not.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from shared.proc import run_bounded

HELPER_BUNDLE_ID = "com.ava.permissions-helper"
"""Fixed across clusters so one TCC grant covers all (the grant keys on this)."""

_READ_TIMEOUT_S = 30.0
"""Bound for one launchctl query — local IPC with launchd."""

_TIMED_OUT_RC = 124
"""Stand-in exit status for a read the bound killed (`timeout(1)`'s convention;
the real launchd codes are far below this)."""


def helper_job_label() -> str:
    """This cluster's helper LaunchAgent label (``<bundle id>.<home slug>``).

    Per-cluster, keyed on the home-path slug (path-only identity); the bundle id
    (the TCC grant) stays shared across clusters.
    """
    from shared.cluster import home_slug
    from shared.paths import ava_home

    return f"{HELPER_BUNDLE_ID}.{home_slug(ava_home())}"


def helper_job_domain() -> str:
    """The launchd domain this login session's jobs live in (``gui/<uid>``)."""
    return f"gui/{os.getuid()}"


def helper_job_agents_dir() -> Path:
    return Path.home() / "Library" / "LaunchAgents"


def helper_job_plist_path() -> Path:
    return helper_job_agents_dir() / f"{helper_job_label()}.plist"


def read_helper_job() -> str | None:
    """One ``launchctl print`` dump of this cluster's helper job.

    None when launchd has no such job — and equally when the read itself could
    not run (a call the bound killed): every caller's next question is what the
    job state says, and a read that says nothing reads as an absent job.
    """
    cmd = ["launchctl", "print", f"{helper_job_domain()}/{helper_job_label()}"]
    try:
        proc = run_bounded(cmd, timeout=_READ_TIMEOUT_S, capture_output=True)
    except subprocess.TimeoutExpired:
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.decode(errors="replace")


@dataclass(frozen=True, slots=True)
class HelperJobState:
    """The launchd facts the helper healthcheck classifies from."""

    job_state: str | None
    """``job state`` value — the line that carries ``spawn failed``."""

    last_exit_code: str | None
    """``last exit code`` value (e.g. ``78: EX_CONFIG``); None before any exit."""

    needs_lwcr_update: bool
    """Whether the dump carries the ``needs LWCR update`` properties marker."""

    btm_uuid: str | None
    """The job's recorded BTM uuid, when launchd shows one."""

    runs: int | None
    """Launchd's spawn count, when the dump shows one."""


_JOB_STATE_RE = re.compile(r"^\s*job state = (.+)$", re.MULTILINE)
_LAST_EXIT_RE = re.compile(r"^\s*last exit code = (.+)$", re.MULTILINE)
_BTM_UUID_RE = re.compile(r"^\s*BTM uuid = (.+)$", re.MULTILINE)
_RUNS_RE = re.compile(r"^\s*runs = (\d+)$", re.MULTILINE)

_NEEDS_LWCR_MARKER = "needs LWCR update"


def parse_job_state(text: str) -> HelperJobState:
    """Parse one ``launchctl print`` dump; unknown lines are ignored (pure)."""
    return HelperJobState(
        job_state=_capture(_JOB_STATE_RE, text),
        last_exit_code=_capture(_LAST_EXIT_RE, text),
        needs_lwcr_update=_NEEDS_LWCR_MARKER in text,
        btm_uuid=_capture(_BTM_UUID_RE, text),
        runs=_runs(_RUNS_RE, text),
    )


def _capture(pattern: re.Pattern[str], text: str) -> str | None:
    match = pattern.search(text)
    return None if match is None else match.group(1).strip()


def _runs(pattern: re.Pattern[str], text: str) -> int | None:
    match = pattern.search(text)
    return None if match is None else int(match.group(1))
