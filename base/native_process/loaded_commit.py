"""Process-commit — the commit a *running process* actually loaded.

The other commit signals are **disk** state: ``head_sha`` is what the
checkout is at right now, ``running_sha`` what ``ava start`` last started on
(`base.deploy.git.running_sha`). Neither answers "what is this daemon executing?" — a
bookmark can be rewritten while an already-live process keeps its old code, so
a daemon can sit on code from days ago while every bookmark reads as current.
That is not hypothetical: on 2026-07-26 a Windows unit's ops daemon served a
capability set it had cached at boot for two days after the file it derives
from changed, and three separate signals agreed it was aligned.

Each executable entry captures one immutable `LoadedCommit` when it loads its
code and passes that fact to its consumers. Reading `sha` never reads Git or
substitutes the current checkout. Unknown stays unknown even if the checkout
later becomes available; a new process entry owns a new capture.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from base.native_process.os_platform import CREATE_NO_WINDOW

_SOURCE_ROOT = Path(__file__).resolve().parents[2]
_GIT_TIMEOUT_S = 10


def capture_commit(source_root: Path) -> str | None:
    """Read the source commit once at an explicit process entry point."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=source_root,
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
    return result.stdout.strip() or None


@dataclass(frozen=True)
class LoadedCommit:
    """One process entry's captured source image, including an honest unknown.

    Consumers read ``sha`` rather than Git. A missing capture stays missing even
    when the checkout later becomes available or moves underneath the process.
    """

    source_root: Path
    sha: str | None

    @classmethod
    def capture(cls, source_root: Path = _SOURCE_ROOT) -> LoadedCommit:
        """Capture at process startup; subsequent use never reads HEAD again."""
        return cls(source_root=source_root, sha=capture_commit(source_root))
