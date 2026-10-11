"""Code version — the integer that orders this process's code against the cluster's minimum.

The version is the number of first-parent commits reachable from the commit the
process loaded (`git rev-list --count --first-parent <sha>`). On `main`, whose
history is linear through squash merges, every merge raises it by exactly one,
so a larger number is always newer code. It needs no release process and no
bump discipline, the same property `base.deploy.git.host_version` has, but it
orders commits inside one day, which a `YYYY.M.D` date cannot.

An entry passes its immutable `LoadedCommit` to `CodeVersion`. It computes and
caches the count for that captured SHA, never for whatever the checkout became
since. Database factories share the same owner throughout their entry lifetime.

It fails fast. A tree that is not a git checkout (a wheel, a tarball) has no
version, and a missing version is not `0`: substituting one would make every
process look older than every minimum, or newer than none. A shallow clone
counts only the commits it holds; that under-reports and can only make the
process look older, which the gate then refuses loudly.

The database gate's admission posture belongs to its entry-owned `ProcessDbGate`.
Operator factories are explicitly exempt; this module holds no mutable process
posture or cached process-global version.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from base.native_process import loaded_commit
from base.native_process.os_platform import CREATE_NO_WINDOW

_GIT_TIMEOUT_S = 10


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
