"""Acquire an install source into a package directory.

Shared by `ava plugins install` (git only) and `ava mcp install` (git or local
path). A git source is cloned into a fresh temp dir; a local path is used in
place (never moved — the caller copies out of it). The caller resolves the
`--path` subdir and then moves / copies the package into its load dir.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

_TEMP_PREFIX = "ava-install-"


class SourcePathNotFoundError(ValueError):
    """The recorded source is neither a git URL nor a path still on disk.

    Raised by `acquire_source` for a local-path source whose directory has
    been deleted (e.g. an MCP package installed from a git worktree that has
    since been removed — audit round 2, skills-plugins #4). The caller prints
    the message instead of a mysterious `git clone` failure of a path that is
    not a repo.
    """


def looks_like_local_path(source: str) -> bool:
    """True when `source` is an existing local directory rather than a git URL.

    A `scheme://` URL (including `file://`) or an `scp`-style `git@host:repo`
    is always a git source; anything else is treated as a local path when it
    resolves to a directory on disk.
    """
    if "://" in source or source.startswith("git@"):
        return False
    return Path(source).expanduser().is_dir()


def clone_git(url: str, ref: str | None) -> Path:
    """Clone `url` into a fresh temp dir (checked out at `ref` when given);
    return the cloned package directory. Caller cleans up with `cleanup_temp`.

    Raises:
        subprocess.CalledProcessError: git clone / checkout failed.
    """
    tmp = Path(tempfile.mkdtemp(prefix=_TEMP_PREFIX))
    dest = tmp / "pkg"
    subprocess.run(["git", "clone", url, str(dest)], check=True, capture_output=True, text=True)
    if ref:
        subprocess.run(
            ["git", "-C", str(dest), "checkout", ref], check=True, capture_output=True, text=True
        )
    return dest


def cleanup_temp(cloned: Path | None) -> None:
    """Remove the temp parent created by `clone_git`, if any (no-op for a local
    source, whose `root` the caller never owns)."""
    if cloned is None:
        return
    parent = cloned.parent
    if parent.name.startswith(_TEMP_PREFIX) and parent.exists():
        shutil.rmtree(parent, ignore_errors=True)


@dataclass
class AcquiredSource:
    """A resolved install source. `root` is the package tree; `cloned` is the
    cloned dir to pass to `cleanup_temp` (None for a local-path source, which is
    left untouched)."""

    root: Path
    cloned: Path | None


def acquire_source(source: str, ref: str | None) -> AcquiredSource:
    """Resolve `source` (git URL or local dir) to a package tree on disk.

    Raises:
        subprocess.CalledProcessError: a git source failed to clone / checkout.
    """
    if looks_like_local_path(source):
        return AcquiredSource(root=Path(source).expanduser().resolve(), cloned=None)
    if "://" not in source and not source.startswith("git@"):
        # Not a git URL and no such directory on disk: a local-path source
        # whose directory no longer exists. `git clone` would fail with a
        # confusing "does not appear to be a git repository"; name the actual
        # problem instead.
        raise SourcePathNotFoundError(
            f"{source!r} is not a git URL and no such local directory exists — "
            f"a local-path source must still be on disk to re-fetch"
        )
    cloned = clone_git(source, ref)
    return AcquiredSource(root=cloned, cloned=cloned)
