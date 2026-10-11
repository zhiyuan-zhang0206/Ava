"""Prod-source git introspection — the checkout a source-run home executes.

The prod source (`$AVA_HOME/source`) is the tree a source-run home's services
run out of. Facts about it that status surfaces show:

- `prod_source_head_sha()` — its HEAD commit, reported per host in the roster
  beside the commit the answering process loaded (`base.native_process.loaded_commit`).
- `checkout_head_sha(repo)` — the same read for an explicit checkout.
- `prod_source_branch_drift()` — its current branch when it is not `main`, i.e.
  an agent developed *in* the prod tree instead of a worktree (un-reviewed code
  on the running host).

All are local, read-only git subprocess calls against a fixed path, with no
dependency on the CLI or gateway layers, so the gateway roster and the ops
status probe can share them with `ava status`.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from base.deploy.git.gitenv import git_env
from base.host.proc import run_bounded

# These reads are local-only (rev-parse / merge-base against an existing
# checkout), so a low ceiling is right — a status render waits on them. Bounded
# via `run_bounded` all the same: on Windows the direct child is
# Git-for-Windows' launcher stub, so a plain `subprocess.run` timeout would kill
# the stub and leave the real git behind on every expiry, and a status probe runs
# often enough to accumulate them.
_GIT_TIMEOUT_S = 5.0


def prod_source_dir() -> Path | None:
    """The installed source checkout of this unit: `$AVA_HOME/source`.

    Every unit runs from its own `$AVA_HOME/source` checkout (source mode), so the
    home names its checkout; a home without one (a test home) reads as absent."""
    from base.paths import ava_home

    return ava_home() / "source"


def _git_ro(*args: str, repo: Path | None = None) -> str | None:
    """Run a read-only git command in the prod source checkout, returning trimmed
    stdout.

    `repo` overrides the checkout the command runs in — the health preflight
    checks the checkout a start is running FROM, which is the prod source on a
    prod install but a worktree elsewhere. Returns None when the checkout is
    absent / not a git repo / git is unavailable / the command fails — every
    caller treats "cannot read" as "nothing to report" rather than an error.
    """
    source = repo if repo is not None else prod_source_dir()
    if source is None or not (source / ".git").exists():
        return None
    try:
        result = run_bounded(  # git + fixed path + literal args, no user input
            ["git", "-C", str(source), *args],
            capture_output=True,
            text=True,
            env=git_env(),
            timeout=_GIT_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def running_from_prod_source() -> bool:
    """Whether the calling process loaded its code from the prod source checkout.

    Every "checkout vs running code" comparison needs this: the checkout facts
    (`prod_source_head_sha`) are read from the installed prod source, while
    `base.native_process.loaded_commit` reports the tree the *process* was loaded from. On prod
    those are the same tree and the comparison is meaningful; in a dev worktree
    they are two different checkouts, so a difference says nothing about drift.
    Returns False when the prod source cannot be resolved — unknown layout, so no
    comparison is licensed.
    """
    source = prod_source_dir()
    if source is None:
        return False
    try:
        # base/deploy/git/cluster_drift.py -> the checkout root.
        return Path(__file__).resolve().parents[3] == source.resolve()
    except OSError:
        return False


def prod_source_head_sha() -> str | None:
    """The prod source's current HEAD commit sha, or None if it cannot be read."""
    return _git_ro("rev-parse", "HEAD")


def checkout_head_sha(repo: Path) -> str | None:
    """`repo`'s current HEAD commit sha, or None if it cannot be read."""
    return _git_ro("rev-parse", "HEAD", repo=repo)


def prod_source_branch_drift() -> str | None:
    """The prod source's current branch when it has drifted off `main`, else None.

    The prod source must sit on reviewed `main`. A non-`main` branch means an
    agent developed in the prod tree (a `git checkout -b` there instead of a
    `git worktree`), putting un-reviewed code on the running host. A detached
    HEAD is the source-mode release state — fleet_update materializes every
    checkout as `--detach NEW` — so consumers treat it as the designed steady
    state, not drift, and report it as the literal branch `"HEAD"`.
    """
    branch = _git_ro("rev-parse", "--abbrev-ref", "HEAD")
    return branch if branch and branch != "main" else None
