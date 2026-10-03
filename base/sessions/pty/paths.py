"""Locations and bounds shared by the pty-sessions service and its clients.

Everything lives under ``$AVA_HOME/run/``: the service's unix socket
(``pty-sessions.sock``), its instance lock (``pty-sessions.lock``) and its ledger
(``pty-sessions.json``); transcripts stay at ``$AVA_HOME/logs/<name>.out.log``.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

# NOTE: `base.paths` is imported inside each path resolver, never at module top:
# importing it builds the pydantic Settings singleton, and the read-only scans that
# pass their run directory explicitly (the worktree guard) must not pay for it.

# AF_UNIX sun_path bound: 104 bytes on macOS, 108 on Linux. A deeply nested
# $AVA_HOME (a long worktree path) pushes the natural socket path over it; the
# service then lives at a short fixed path keyed by the home, which the service
# and every client compute identically.
SUN_PATH_MAX = 100

# The roster unit (and root unit id) of the pty-sessions service.
SERVICE_UNIT = "pty-sessions"

# Terminal geometry defaults (the classic pane shape). Defined here so the service
# reads them without importing the screen module, whose import pulls pyte (kept lazy
# until a capture needs it).
DEFAULT_COLS = 120
DEFAULT_ROWS = 40

# Wire-protocol payload bounds for one capture/resize request
# (task #3696 exception inventory): protective limits, not user-facing tuning knobs — the
# configurable surface is only the capture *default* window
# (display.shell_capture_default_lines). The service clamps to them, and the client
# validates against CAPTURE_MAX_LINES before dialing, so one request can never
# ask for an unbounded payload.
CAPTURE_MAX_LINES = 100000
RESIZE_MAX = 10000


def service_socket_path(run: Path | None = None) -> Path:
    """The pty-sessions service's unix socket; ``$AVA_HOME/run/pty-sessions.sock``.

    `run` names the run directory explicitly (a read-only scan that must not create
    the home); the default is this process's own, created on first use.

    A path over the sun_path bound falls back to ``/tmp/ava-pty-<uid>/<digest>.sock``,
    keyed by the run directory. The directory is fixed rather than
    ``tempfile.gettempdir()`` because the service (launched by root) and its clients
    (agent processes with their own TMPDIR) must compute the same path; the service
    creates the directory owner-only (`fallback_dir`).
    """
    if run is None:
        from base.paths import run_dir

        run = run_dir()
    natural = run / "pty-sessions.sock"
    if len(str(natural)) <= SUN_PATH_MAX:
        return natural
    digest = hashlib.sha1(f"{run}\0pty-sessions".encode(), usedforsecurity=False).hexdigest()
    return fallback_dir() / f"{digest[:12]}.sock"


def fallback_dir() -> Path:
    """Where a socket too long for its home lives: ``/tmp/ava-pty-<uid>`` (owner-only)."""
    return Path("/tmp") / f"ava-pty-{os.getuid()}"  # noqa: S108 — a fixed, uid-keyed, owner-only directory: every process of the user must compute the same path


def lock_path() -> Path:
    """The service's instance lock (``run/pty-sessions.lock``), held while it runs."""
    from base.paths import run_dir

    return run_dir() / "pty-sessions.lock"


def ledger_path() -> Path:
    """The service's own ledger of live shell identities (``run/pty-sessions.json``)."""
    from base.paths import run_dir

    return run_dir() / "pty-sessions.json"


def transcript_path(name: str) -> Path:
    """The session's byte transcript ($AVA_HOME/logs/<name>.out.log)."""
    from base.paths import logs_dir

    return logs_dir() / f"{name}.out.log"
