"""Prod-source git introspection — the checkout the live cluster runs from.

The prod source (`$AVA_HOME/source`) is the tree every cluster service runs out
of. Two facts about it surface "this host is running something other than the
reviewed, pinned code":

- `prod_source_head_sha()` — its HEAD commit, compared against the cluster pin
  (`shared.cluster_pin.get_cluster_target_sha`) to detect a node that has drifted
  off the commit the whole cluster should be on.
- `prod_source_branch_drift()` — its current branch when it is not `main`, i.e.
  an agent developed *in* the prod tree instead of a worktree (un-reviewed code
  on the running host; the next rollout force-discards it).

All are subprocess calls against a fixed path — local reads plus one
best-effort fetch (`prod_source_fetch`, the only one that touches the network),
with no dependency on the CLI or gateway layers, so the watchdog (a `services`
daemon, which may not import `cli`) and the gateway roster can share them with
`ava status`.
"""

from __future__ import annotations

import contextlib
import subprocess
from pathlib import Path
from typing import Literal

from shared.gitenv import git_env
from shared.proc import run_bounded

# These reads are local-only (rev-parse / merge-base against an existing
# checkout), so a low ceiling is right — a status render waits on them. Bounded
# via `run_bounded` all the same: on Windows the direct child is
# Git-for-Windows' launcher stub, so a plain `subprocess.run` timeout would kill
# the stub and leave the real git behind on every expiry, and a status probe runs
# often enough to accumulate them.
_GIT_TIMEOUT_S = 5.0

# `prod_source_fetch`'s ceiling. A fetch is network I/O with no natural bound (a
# wedged network hangs git until TCP gives up), so it cannot share the local-read
# ceiling; 30s is generous enough for a real fetch while small enough that the
# watchdog tick that runs it (the pin-drift unknown branch) is delayed, not
# parked, when the remote is unreachable.
_FETCH_TIMEOUT_S = 30.0


def prod_source_dir() -> Path | None:
    """The installed prod source checkout, layout-independent.

    Resolved from `$AVA_HOME/source` first, with the host `ava` symlink
    (`~/.local/bin/ava` → `<source>/.venv/bin/ava`) as fallback. The home-first
    order is load-bearing for co-located clusters (e.g. a preview gateway
    `~/.ava-preview` on the same host as the prod runner `~/.ava`): the symlink
    points at PROD's source on every unit layout, so reading it from the
    secondary unit would report PROD's HEAD as its own and raise a false
    off-pin warning. The symlink fallback still covers the gateway-only layout
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


def _git_rc(*args: str, repo: Path | None = None) -> int | None:
    """Run a read-only git command in the prod source checkout, returning its exit
    code rather than stdout.

    `repo` overrides the checkout the command runs in, like `_git_ro`. Lets a
    caller act on a meaningful non-zero exit -- `git merge-base
    --is-ancestor A B` exits 0 (A is an ancestor of B), 1 (it is not), or >1
    (neither commit could be resolved) -- which `_git_ro` collapses into None.
    Returns None only when the command could not be run at all (checkout absent /
    not a git repo / git unavailable).
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
    return result.returncode


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
    """The prod source's current HEAD commit sha, or None if it cannot be read.

    Compared against the cluster pin (`cluster_target_sha`) to surface a node
    drifted off the cluster's pinned commit.
    """
    return _git_ro("rev-parse", "HEAD")


PinRelation = Literal["aligned", "ahead", "behind", "diverged", "unknown"]


def prod_source_pin_relation(pin: str, head: str, *, repo: Path | None = None) -> PinRelation:
    """How a checkout's HEAD relates to the cluster pin, via git ancestry:

    - "aligned"  — HEAD is the pin.
    - "behind"   — HEAD is an ancestor of the pin: this host missed a rollout, and
                   `ava cluster update` fast-forwards it onto the pin.
    - "ahead"    — the pin is an ancestor of HEAD: the checkout moved past the pin
                   — a stray `git pull`, or a rollout that landed while the pin was
                   not advanced (the convergent end-state of a mid-rollout failure).
                   The pin is a floor, not a ceiling: nothing resets the tree back
                   to it. `ava cluster update` force-checks-out the reviewed target
                   (origin/main, resolved once) and advances the pin to it — a
                   stray HEAD is discarded, not promoted.
    - "diverged" — neither is an ancestor of the other (a rebase / force-push).
    - "unknown"  — the relationship can't be computed: the pin commit is not present
                   in this checkout (never fetched), or git can't be read.

    Defaults to the prod source checkout (the tree the live cluster runs out of);
    `repo` overrides it for a caller checking a different checkout (the health
    preflight checks the checkout a start is running FROM). Equality is checked
    first, so "ahead"/"behind" never collapse onto an equal pair.
    """
    if head == pin:
        return "aligned"
    pin_is_ancestor = _git_rc("merge-base", "--is-ancestor", pin, head, repo=repo)
    head_is_ancestor = _git_rc("merge-base", "--is-ancestor", head, pin, repo=repo)
    if pin_is_ancestor == 0:
        return "ahead"
    if head_is_ancestor == 0:
        return "behind"
    if pin_is_ancestor == 1 and head_is_ancestor == 1:
        return "diverged"
    return "unknown"


def prod_source_branch_drift() -> str | None:
    """The prod source's current branch when it has drifted off `main`, else None.

    The prod source must sit on reviewed `main`. A non-`main` branch means an
    agent developed in the prod tree (a `git checkout -b` there instead of a
    `git worktree`), putting un-reviewed code on the running host. A detached
    HEAD reports as the literal branch `"HEAD"`.
    """
    branch = _git_ro("rev-parse", "--abbrev-ref", "HEAD")
    return branch if branch and branch != "main" else None


def prod_source_fetch(*refs: str, repo: Path | None = None) -> bool:
    """Best-effort `git fetch <refs...>` in the prod source checkout.

    The one non-read in this module: it writes to the object store and
    FETCH_HEAD (never the working tree — concurrent with a checkout it is the
    fetch half of a `git pull`, which git already serializes). Its purpose is
    to make a pin commit locally resolvable before judging ancestry:
    `prod_source_pin_relation` answers "unknown" when the pin was never
    fetched, and a checkout cannot be healed toward a commit it cannot see
    (the pin-drift self-heal's unknown branch). Returns False when the checkout
    is absent / not a git repo / git is unavailable / the fetch fails or times
    out — the caller keeps its prior judgment then.
    """
    source = repo if repo is not None else _prod_source_dir()
    if source is None or not (source / ".git").exists():
        return False
    try:
        result = run_bounded(  # git + fixed path + literal refs, no user input
            ["git", "-C", str(source), "fetch", *refs],
            capture_output=True,
            text=True,
            env=git_env(),
            timeout=_FETCH_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0
