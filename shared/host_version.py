"""Host version — derived from the checkout's commit, never hand-maintained.

The host version is the date axis of the running build: `YYYY.M.D` from the
commit date of the checkout's HEAD, with `+g<short-sha>` added in the display
form. It always exists on every checkout-mode machine, advances by itself,
and needs no release process and no bump discipline (task #2915 design §5.5
v3 — the hand-maintained `[project].version` was rejected as the gate source;
it survives only as the checkout-less fallback).

The gates compare against the bare `YYYY.M.D` string — `engines.ava` ranges
via `shared.plugin_manifest.range_allows` plus the `requires_commit` ancestor
check. Prefer the display form only where a human reads it (status output,
log lines).

Fallback chain: git-derived date -> `[project].version` from `pyproject.toml`
(the checkout-less / wheel case) -> `HostVersionError` (callers surface
"unknown" rather than guessing a number a gate would trust).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from shared.gitenv import git_env
from shared.paths import repo_root as _repo_root
from shared.proc import run_bounded

_GIT_TIMEOUT_S = 10.0


class HostVersionError(RuntimeError):
    """No host version could be derived — not a git checkout and no usable
    `pyproject.toml` version. Callers must not substitute a default."""


def _derived(repo: Path) -> tuple[str, str] | None:
    """(bare version, short sha) from `repo`'s HEAD commit, or None."""
    try:
        result = run_bounded(  # git + fixed args, no user input
            ["git", "-C", str(repo), "log", "-1", "--format=%cd|%h", "--date=short"],
            capture_output=True,
            text=True,
            env=git_env(),
            timeout=_GIT_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    date_part, _, sha = result.stdout.strip().partition("|")
    try:
        year, month, day = date_part.split("-")
        return f"{int(year)}.{int(month)}.{int(day)}", sha
    except ValueError:
        return None


def host_version(repo: Path | None = None) -> str:
    """The gate-comparable host version: bare `YYYY.M.D`.

    Raises:
        HostVersionError: no git commit and no readable pyproject version.
    """
    repo = repo or _repo_root()
    derived = _derived(repo)
    if derived is not None:
        return derived[0]
    from shared import plugin_manifest

    try:
        return plugin_manifest.host_version_from_repo(repo)
    except plugin_manifest.ManifestError as e:
        raise HostVersionError(str(e)) from e


def host_version_display(repo: Path | None = None) -> str:
    """The human-facing form: `YYYY.M.D+g<short-sha>` when the checkout is a
    git repo, bare otherwise (falls back through `host_version`)."""
    repo = repo or _repo_root()
    derived = _derived(repo)
    if derived is not None:
        return f"{derived[0]}+g{derived[1]}"
    return host_version(repo)
