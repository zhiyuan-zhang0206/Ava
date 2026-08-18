"""Process-commit — the commit a *running process* actually loaded.

The three commit signals that predate this module are all **disk** state:
``head_sha`` is what the checkout is at right now, ``installed_sha`` what was
last ``uv sync``'d (`shared.source_integrity`), ``running_sha`` what ``ava
start`` last started on (`shared.running_sha`). None of them answers "what is
this daemon executing?" — ``ava start`` is skip-if-running, so it rewrites the
bookmarks and leaves already-live processes alone. A daemon can therefore sit
on code from days ago while all three bookmarks read as current, and every
status surface reports green. That is not hypothetical: on 2026-07-26 a
Windows unit's ops daemon served a capability set it had cached at boot for two
days after the file it derives from changed, and three separate signals agreed
it was aligned.

This module is **process** state, and it gets that property from two rules:

- ``freeze()`` resolves the commit of the tree *this module was loaded from*
  (``__file__``, not the cwd) and is called once at process boot.
- ``get()`` only ever returns what ``freeze()`` stored. It never falls back to
  reading git, because a lazy read inside a long-lived process would re-answer
  with whatever the checkout has since become — reproducing the exact false
  green this module exists to expose. A process that never froze reports
  unknown, and an unknown is a useful answer; a confident wrong one is not.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from shared.platform import CREATE_NO_WINDOW

_SOURCE_ROOT = Path(__file__).resolve().parent.parent
_GIT_TIMEOUT_S = 10

_frozen: str | None = None


def freeze() -> str | None:
    """Capture the commit of the tree this process loaded, and return it.

    Called once per process, as early in its life as the boot sequence allows;
    idempotent, so a second caller re-uses the first capture rather than taking
    a fresh reading. Best-effort — a source tree that is not a git checkout (an
    installed package, a tarball deploy) simply has no commit to report."""
    global _frozen  # noqa: PLW0603 — process-lifetime capture, one per process by design
    if _frozen is not None:
        return _frozen
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=_SOURCE_ROOT,
            capture_output=True,
            text=True,
            check=False,
            creationflags=CREATE_NO_WINDOW,
            timeout=_GIT_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    _frozen = result.stdout.strip() or None
    return _frozen


def get() -> str | None:
    """The commit this process loaded, or None when it never froze one."""
    return _frozen


def _reset_for_tests() -> None:
    """Drop the capture so a test can exercise the boot path more than once."""
    global _frozen  # noqa: PLW0603 — test seam for a process-lifetime global
    _frozen = None
