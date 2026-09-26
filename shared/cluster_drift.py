"""Prod-source git introspection — the checkout a source-run home executes.

The prod source (`$AVA_HOME/source`) is the tree a source-run home's services
run out of. Facts about it that status surfaces show:

- `prod_source_head_sha()` — its HEAD commit, reported per host in the roster
  beside the commit the answering process loaded (`shared.process_sha`).
- `checkout_head_sha(repo)` — the same read for an explicit checkout.
- `prod_source_branch_drift()` — its current branch when it is not `main`, i.e.
  an agent developed *in* the prod tree instead of a worktree (un-reviewed code
  on the running host).

All are local, read-only git subprocess calls against a fixed path, with no
dependency on the CLI or gateway layers, so the gateway roster and the ops
status probe can share them with `ava status`.
"""

from __future__ import annotations

import contextlib
import subprocess
from pathlib import Path

from shared.deploy.git.gitenv import git_env
from shared.proc import run_bounded

# These reads are local-only (rev-parse / merge-base against an existing
# checkout), so a low ceiling is right — a status render waits on them. Bounded
# via `run_bounded` all the same: on Windows the direct child is
# Git-for-Windows' launcher stub, so a plain `subprocess.run` timeout would kill
# the stub and leave the real git behind on every expiry, and a status probe runs
# often enough to accumulate them.
_GIT_TIMEOUT_S = 5.0


def prod_source_dir() -> Path | None:
    """The installed prod source checkout, layout-independent.

    Resolved from `$AVA_HOME/source` first, with the host `ava` symlink
    (`~/.local/bin/ava` → `<source>/.venv/bin/ava`) as fallback. The home-first
    order is load-bearing for co-located clusters (e.g. a preview gateway
    `~/.ava-preview` on the same host as the prod runner `~/.ava`): the symlink
    points at PROD's source on every unit layout, so reading it from the
    secondary unit would report PROD's HEAD as its own. The symlink fallback still covers the gateway-only layout
    (`$AVA_HOME=~/.ava_gateway` with the checkout at `/opt/ava/source`), where
    `$AVA_HOME/source` does not exist. The symlink is never repointed by a dev
    cluster, so from a dev worktree this still reports PROD's source (the
    worktree's own home has no `source/` child and its checkout is not under
    `~/.local/bin/ava`)."""
    from shared.paths import ava_home

    home_source = ava_home() / "source"
    if (home_source / ".git").exists():
        return home_source
    link = Path.home() / ".local" / "bin" / "ava"
    with contextlib.suppress(OSError, IndexError):
        if link.is_symlink():
            # <source>/.venv/bin/ava → parents: [bin, .venv, <source>]
            return link.resolve().parents[2]
    return home_source


# Legacy private spelling — kept so existing monkeypatches keep resolving.
_prod_source_dir = prod_source_dir


def _git_ro(*args: str, repo: Path | None = None) -> str | None:
    """Run a read-only git command in the prod source checkout, returning trimmed
    stdout.

    `repo` overrides the checkout the command runs in — the health preflight
    checks the checkout a start is running FROM, which is the prod source on a
    prod install but a worktree elsewhere. Returns None when the checkout is
    absent / not a git repo / git is unavailable / the command fails — every
    caller treats "cannot read" as "nothing to report" rather than an error.
    """
    source = repo if repo is not None else _prod_source_dir()
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
    `shared.process_sha` reports the tree the *process* was loaded from. On prod
    those are the same tree and the comparison is meaningful; in a dev worktree
    they are two different checkouts, so a difference says nothing about drift.
    Returns False when the prod source cannot be resolved — unknown layout, so no
    comparison is licensed.
    """
    source = _prod_source_dir()
    if source is None:
        return False
    try:
        return Path(__file__).resolve().parents[1] == source.resolve()
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
    HEAD reports as the literal branch `"HEAD"`.
    """
    branch = _git_ro("rev-parse", "--abbrev-ref", "HEAD")
    return branch if branch and branch != "main" else None
