"""Code version — the integer that orders this process's code against the cluster's minimum.

The version is the number of first-parent commits reachable from the commit the
process loaded (`git rev-list --count --first-parent <sha>`). On `main`, whose
history is linear through squash merges, every merge raises it by exactly one,
so a larger number is always newer code. It needs no release process and no
bump discipline, the same property `base.deploy.git.host_version` has, but it
orders commits inside one day, which a `YYYY.M.D` date cannot.

It is computed from the commit `loaded_commit.freeze()` captured at boot, never
from whatever the checkout has become since: a daemon that outlived a checkout
move must report the code it executes, or the gate that reads this would let it
through. The result is cached for the process lifetime.

It fails fast. A tree that is not a git checkout (a wheel, a tarball) has no
version, and a missing version is not `0`: substituting one would make every
process look older than every minimum, or newer than none. A shallow clone
counts only the commits it holds; that under-reports and can only make the
process look older, which the gate then refuses loudly.

This module also holds the process's posture toward the database gate
(`base.db.code_version_gate`): every process is subject to it unless its entry
point calls `exempt_from_db_gate()`. It lives here, not beside the gate,
because the operator CLI entry point must declare its posture before it builds
Settings, and importing `base.db` builds them.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from base.native_process import loaded_commit
from base.native_process.os_platform import CREATE_NO_WINDOW

# The tree this module was loaded from (not the cwd), the same anchor
# `loaded_commit` freezes.
_SOURCE_ROOT = Path(__file__).resolve().parents[2]
_GIT_TIMEOUT_S = 10

_version: int | None = None
_db_gate_exempt = False


class CodeVersionError(RuntimeError):
    """This process's code version cannot be determined."""


def first_parent_count(repo: Path, rev: str = "HEAD") -> int:
    """The number of first-parent commits reachable from `rev` in `repo`.

    Raises:
        CodeVersionError: `repo` is not a git checkout, `rev` does not resolve,
            git is missing, or git does not answer in time.
    """
    argv = ["git", "rev-list", "--count", "--first-parent", rev]
    try:
        result = subprocess.run(  # noqa: S603 — git + fixed flags; `rev` is a captured sha
            argv,
            cwd=repo,
            capture_output=True,
            text=True,
            check=False,
            creationflags=CREATE_NO_WINDOW,
            timeout=_GIT_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise CodeVersionError(f"`{' '.join(argv)}` did not run in {repo}: {exc}") from exc
    if result.returncode != 0:
        raise CodeVersionError(
            f"`{' '.join(argv)}` failed in {repo} (exit {result.returncode}): "
            f"{result.stderr.strip() or 'no output'}"
        )
    try:
        return int(result.stdout.strip())
    except ValueError as exc:
        raise CodeVersionError(
            f"`{' '.join(argv)}` in {repo} did not print a commit count: {result.stdout!r}"
        ) from exc


def get() -> int:
    """This process's code version, computed once and cached.

    Raises:
        CodeVersionError: the process's source tree is not a git checkout.
    """
    global _version  # noqa: PLW0603 — process-lifetime cache, one per process by design
    if _version is None:
        sha = loaded_commit.freeze()
        if sha is None:
            raise CodeVersionError(
                f"cannot resolve the commit of {_SOURCE_ROOT}: the code version is the "
                "first-parent commit count, so the process must run from a git checkout"
            )
        _version = first_parent_count(_SOURCE_ROOT, sha)
    return _version


def exempt_from_db_gate() -> None:
    """Declare this process an operator tool that the database gate must not stop.

    Only the `ava` CLI entry point calls this. `ava stop` writes to the
    database to drain agents, so a gate that also stopped it would leave a
    host running stale code unable to run the very command that stops it.
    Service processes never call it: they are the writers the gate exists to stop.
    """
    global _db_gate_exempt  # noqa: PLW0603 — process posture, declared once at the entry point
    _db_gate_exempt = True


def db_gate_applies() -> bool:
    """Whether this process's pooled database sessions enforce the cluster minimum."""
    return not _db_gate_exempt


class CodeVersion:
    """The lazy integer version of one explicitly captured process image."""

    def __init__(self, loaded: loaded_commit.LoadedCommit) -> None:
        self.loaded = loaded
        self._version: int | None = None

    def get(self) -> int:
        """Resolve only the captured SHA, without consulting the current HEAD."""
        if self._version is None:
            if self.loaded.sha is None:
                raise CodeVersionError(
                    f"cannot resolve the loaded commit of {self.loaded.source_root}: "
                    "the process must capture its code version at startup"
                )
            self._version = first_parent_count(self.loaded.source_root, self.loaded.sha)
        return self._version
