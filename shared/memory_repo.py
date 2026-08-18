"""Memory pool git ops — init / status / branch name management.

Two orthogonal checkouts serve the two capabilities:

- **Agent-runner** (`memory_dir()` → `$AVA_HOME/memory`, branch
  `machine-<name>` via `branch_name()`): agents write notes here; a nightly
  steward commits + pushes + opens a PR `machine-<name>` → `main`.
- **Gateway** (`gateway_memory_dir()` → `$AVA_HOME/gateway/memory` on a
  combined unit, same as `memory_dir()` on a gateway-only unit; always
  tracks `main` via `init_gateway()`): the memory indexer watches this
  checkout. After the arbiter merges per-machine PRs into `main`, the
  gateway refresh endpoint calls `pull_main()` to fast-forward it, and the
  indexer re-embeds the changed files.

On a combined unit (gateway+agent-runner on one host), the two paths are
separate — no cross-contamination. On a split deployment, each unit has
exactly one path matching its role.

This module handles:
- read `$AVA_HOME/memory_remote` for the remote URL
- check local repo state (is it a git repo / is the branch correct)
- first-time init for each checkout (`init()` for agent-runner,
  `init_gateway()` for gateway)
- `pull_main()`: fast-forward the gateway checkout to origin/main
- status report (used by CLI `ava memory status`)

When `AVA_MEMORY_KEEP_LOCAL` is set, each checkout is a local-only git repo:
init does `git init` with no remote (and strips any remote left from a prior
remote-backed setup), `pull_main()` is a no-op, and the nightly push / PR /
merge flow simply does not run. Notes stay on this host.

Not responsible for: the nightly commit/push/PR (the steward agent's
job) or the cross-machine merge (the arbiter agent's job) — both are
agent-driven through the `ava-memory` skill, not daemons here.
"""

from __future__ import annotations

import contextlib
import datetime
import subprocess
from dataclasses import dataclass
from pathlib import Path

from shared.config import settings
from shared.machine import is_agent_runner, is_gateway, machine_name
from shared.paths import ava_home, gateway_memory_dir, memory_dir
from shared.platform import CREATE_NO_WINDOW
from shared.proc import run_bounded

# Memory pool physical path is `$AVA_HOME/memory` via `memory_dir()` (same
# location `ava/memory.py:PATH` exposes to the agent). We resolve it through
# shared.paths rather than importing `ava.memory` (shared is the foundation of
# the ava SDK; shared importing ava would be a cycle).


_DEFAULT_GITIGNORE = """\
# Ava memory pool — markdown notes only; swap / cache files do not enter history.
.DS_Store
*.swp
*.swo
*~
.cache/
"""


class MemoryRemoteMissing(RuntimeError):  # noqa: N818 — "state description" naming, same as MachineNameMissing
    """Neither env `AVA_MEMORY_REMOTE` nor `$AVA_HOME/memory_remote` is set — multi-machine memory setup is incomplete."""


class MemoryRepoUninitialized(RuntimeError):  # noqa: N818
    """`~/.ava/memory` is not yet a git repo — `ava start`'s ensure_memory_repo() should auto-init."""


class MemoryBranchMismatch(RuntimeError):  # noqa: N818
    """The initialized repo is on the wrong branch — `machine_name()` does not match working tree branch."""


def memory_remote() -> str:
    """Get the central git remote URL. settings (env-backed) > file > raise.

    Precedence:
    1. `settings.general.memory_remote` (env `AVA_MEMORY_REMOTE` / headless
       deployment / CI injection)
    2. `$AVA_HOME/memory_remote` file (regular setup; `ava start`
       first run writes it)
    3. neither -> MemoryRemoteMissing (`ava start` catches and
       TTY-prompts to write the file)

    Raises:
        MemoryRemoteMissing: settings.general.memory_remote empty + file
            missing or empty.
    """
    env = settings.general.memory_remote.strip()
    if env:
        return env
    p = ava_home() / "memory_remote"
    if p.exists():
        url = p.read_text().strip()
        if url:
            return url
    raise MemoryRemoteMissing(
        f"memory remote not set — `ava start` will TTY-prompt you to set up "
        f"(default git@github.com:<gh-user>/AvaMemory.git). Manual setup: "
        f"`echo <git-url> > {p}` or `export AVA_MEMORY_REMOTE=<git-url>`."
    )


def branch_name() -> str:
    """This unit's agent-runner memory branch. Returns `machine-<name>` when
    this unit carries the agent-runner capability (even on a combined unit that
    also serves as gateway). Falls back to `main` for a gateway-only unit
    (which has no agent-runner — the consolidated checkout managed by
    init_gateway() always tracks `main`)."""
    if is_agent_runner():
        return f"machine-{machine_name()}"
    return "main"


def pull_main() -> str:
    """Fast-forward the gateway's consolidated memory checkout to origin/main
    and return the resulting HEAD commit sha. Operates on the gateway memory
    path (gateway_memory_dir()) so it always targets the consolidated pool, even
    on a combined unit where the agent-runner's authoring checkout sits at a
    separate path.

    The gateway refresh endpoint calls this to bring its index source up to the
    consolidated pool after the pool's branches are merged. When
    AVA_MEMORY_KEEP_LOCAL is set the checkout has no remote to pull from, so this
    is a no-op that just reports the current HEAD.

    Raises:
        subprocess.CalledProcessError: fetch/merge failure or a non-fast-forward
            (the local checkout diverged from origin/main).
    """
    cwd = gateway_memory_dir()
    from shared.config import settings

    if settings.general.memory_keep_local:
        return _run_git("rev-parse", "HEAD", cwd=cwd)
    branch = settings.general.track_branch
    _run_git("fetch", "origin", branch, cwd=cwd)
    _run_git("merge", "--ff-only", f"origin/{branch}", cwd=cwd)
    return _run_git("rev-parse", "HEAD", cwd=cwd)


def is_initialized() -> bool:
    """Whether the agent-runner memory pool is already a git repo
    (`.git` directory exists)."""
    return (memory_dir() / ".git").is_dir()


def gateway_is_initialized() -> bool:
    """Whether the gateway's consolidated memory checkout is already a git
    repo (`.git` directory exists)."""
    return (gateway_memory_dir() / ".git").is_dir()


# Bounds a single git invocation. Generous because these include network work
# (`fetch` / `ls-remote` / `push` against the memory remote) on a repo of
# markdown notes; the point is not latency but that the call cannot wait
# forever. An unbounded one does not merely stall its own caller: the indexer
# runs `pull_main()` through `asyncio.to_thread`, and `asyncio.run()`'s shutdown
# waits on `shutdown_default_executor()` — so a `git fetch` wedged against a
# dead remote holds the whole daemon's exit open long after SIGTERM arrived,
# until the stop path's SIGKILL fallback. The bound is what keeps a delivered
# signal equal to a prompt exit.
_GIT_TIMEOUT_S = 120.0


def _run_git(*args: str, cwd: Path | None = None) -> str:
    """git wrapper; raise on non-zero or timeout, return stripped stdout.

    Raises:
        subprocess.CalledProcessError: git exited non-zero.
        subprocess.TimeoutExpired: git ran past `_GIT_TIMEOUT_S`. The process
            tree is already dead when this is raised.
    """
    cwd_path = cwd if cwd is not None else memory_dir()
    argv = ["git", *args]
    # run_bounded, not `subprocess.run(timeout=...)`: on Windows the direct child
    # is Git-for-Windows' launcher stub, so a plain timeout kills the stub and
    # leaves the real git running (shared/proc.py).
    result = run_bounded(
        argv,
        timeout=_GIT_TIMEOUT_S,
        cwd=str(cwd_path),
        capture_output=True,
        text=True,
        creationflags=CREATE_NO_WINDOW,
    )
    # run_bounded has no `check=` (a non-zero exit must never be confused with
    # the timeout path), so the raise-on-non-zero half of the contract is here.
    if result.returncode != 0:
        raise subprocess.CalledProcessError(
            result.returncode, argv, output=result.stdout, stderr=result.stderr
        )
    return result.stdout.strip()


def _init_local_repo(branch: str, cwd: Path) -> None:
    """git-init a local-only memory checkout (no remote) at `cwd` on `branch`,
    with an initial .gitignore commit. Used for the offline / keep-local path
    where the pool never connects to a central remote."""
    cwd.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(  # noqa: S603
        ["git", "init", "-q", "-b", branch, str(cwd)], check=True, creationflags=CREATE_NO_WINDOW
    )
    _run_git("config", "user.email", "ava@local", cwd=cwd)
    _run_git("config", "user.name", "Ava Local", cwd=cwd)
    gitignore = cwd / ".gitignore"
    gitignore.write_text(_DEFAULT_GITIGNORE)
    _run_git("add", ".gitignore", cwd=cwd)
    _run_git("commit", "-q", "-m", f"init: local memory on {branch}", cwd=cwd)


def _strip_remotes(cwd: Path) -> None:
    """Remove every git remote from the checkout at `cwd`. Keep-local mode must
    never sync off-box, so a leftover remote (e.g. a host flipped to keep-local
    after being set up against a central remote) is dropped on every converge."""
    for name in _run_git("remote", cwd=cwd).split():
        _run_git("remote", "remove", name, cwd=cwd)


@dataclass(frozen=True)
class RepoStatus:
    """Structured form of `ava memory status` output (CLI prints directly; also convenient for future callers)."""

    branch: str
    is_clean: bool  # working tree + index fully clean (`git status --porcelain` empty)
    local_commits_on_branch: int  # total commits on this branch (reflects "accumulated notes")
    ahead_of_main: int  # commits this branch has beyond `origin/main` (not yet merged)
    behind_main: int  # commits `origin/main` has beyond this branch (need rebase)
    last_fetch: str  # ISO ts of `.git/FETCH_HEAD` mtime, or "never"


def init() -> None:
    """First-time setup of the memory repo — idempotent.

    The branch this unit's checkout tracks is role-aware (`branch_name()`):
    `main` on the gateway, `machine-<name>` on a runner.

    When AVA_MEMORY_KEEP_LOCAL is set, the checkout is local-only: a fresh repo
    is `git init`-ed with no remote, and an already-initialized repo has any
    remote stripped — no clone / push / fetch ever runs.

    Scenarios:
    - Local already git repo + branch correct: noop.
    - Local already git repo but on a different branch:
      - gateway: auto-switch to `main` (it never authors, so a stale
        `machine-<name>` checkout from a prior role has no unpushed work to
        lose).
      - runner (agent-runner): raise MemoryBranchMismatch (do not unilaterally
        checkout, may have unpushed in-flight work; let the user handle).
    - Local missing + memory_remote set: `git clone` central remote
      -> checkout the role's branch -> write .gitignore + commit + push.
    - Local missing + memory_remote not set (e.g. bench container /
      offline personal dev): `git init -b <branch>` local empty repo +
      empty commit; do not connect remote. Later `ava memory push/pull`
      raises MemoryRemoteMissing; the user runs `ava start
      --memory-remote <url>` to reconfigure if they want to sync.

    Raises:
        MachineNameMissing: `$AVA_HOME/machine_name` not set.
        MemoryBranchMismatch: already init'd on a runner but branch incorrect.
        subprocess.CalledProcessError: git command failure (network /
            auth / remote does not exist, etc.).
    """
    branch = branch_name()
    keep_local = settings.general.memory_keep_local
    if is_initialized():
        current = _run_git("rev-parse", "--abbrev-ref", "HEAD")
        if current != branch:
            if is_gateway() and not is_agent_runner():
                # gateway-only unit: the memory checkout is the consolidated
                # pool; an existing checkout on a stale machine-<name> branch
                # is safe to move onto main (it never authors, so there is no
                # unpushed authored work to lose).
                if not keep_local:
                    _run_git("fetch", "origin", "main")
                _run_git("checkout", "main")
                if keep_local:
                    _strip_remotes(memory_dir())
                return
            raise MemoryBranchMismatch(  # agent-runner (or combined): may have unpushed work
                f"{memory_dir()} is already a git repo but on branch {current!r}, expected {branch!r}. "
                f"Manually switch: `git -C {memory_dir()} checkout {branch}` "
                "(confirm working tree has no unpushed in-flight work)."
            )
        if keep_local:
            _strip_remotes(memory_dir())
        return

    if keep_local:
        # local-only pool: a git repo with no remote, never synced off-box
        _init_local_repo(branch, memory_dir())
        return

    memory_dir().parent.mkdir(parents=True, exist_ok=True)
    try:
        remote = memory_remote()
    except MemoryRemoteMissing:
        # No remote: local empty repo (bench / offline dev), no origin
        _init_local_repo(branch, memory_dir())
        return

    # `git clone` into a non-existent directory — cwd is the parent
    subprocess.run(  # noqa: S603
        ["git", "clone", remote, str(memory_dir())],
        check=True,
        creationflags=CREATE_NO_WINDOW,
    )

    # Whether the remote already has this branch (another host or our
    # previous setup pushed it)
    remote_branches = _run_git("ls-remote", "--heads", "origin", branch)
    if remote_branches:
        _run_git("checkout", branch)
    else:
        # Fresh branch — based on the HEAD of the clone (could be
        # main, could be empty repo)
        _run_git("checkout", "-b", branch)
        _run_git("push", "-u", "origin", branch)

    # Write .gitignore (if missing, commit + push)
    gitignore = memory_dir() / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text(_DEFAULT_GITIGNORE)
        _run_git("add", ".gitignore")
        _run_git("commit", "-m", f"init: .gitignore on {branch}")
        _run_git("push")


def init_gateway() -> None:
    """First-time setup of the gateway's consolidated memory checkout — idempotent.

    Always tracks `main` (the consolidated pool). On a combined unit
    (gateway+agent-runner) this checkout lives at `gateway_memory_dir()`
    ($AVA_HOME/gateway/memory), separate from the agent-runner's authoring
    checkout at `memory_dir()`. On a gateway-only unit it is the same path as
    `memory_dir()`.

    When AVA_MEMORY_KEEP_LOCAL is set, the checkout is local-only: a fresh repo
    is `git init`-ed with no remote, and an already-initialized repo has any
    remote stripped — no clone / push / fetch ever runs.

    Scenarios:
    - Local already git repo + on `main`: noop.
    - Local already git repo but on a different branch: auto-switch to
      `main` (the gateway never authors, so nothing is lost).
    - Local missing + memory_remote set: `git clone` central remote
      -> checkout `main` -> write .gitignore + commit + push.
    - Local missing + memory_remote not set: `git init -b main` local
      empty repo + empty commit; do not connect remote.

    Raises:
        subprocess.CalledProcessError: git command failure (network /
            auth / remote does not exist, etc.).
    """
    branch = "main"
    gmd = gateway_memory_dir()
    keep_local = settings.general.memory_keep_local
    if gateway_is_initialized():
        current = _run_git("rev-parse", "--abbrev-ref", "HEAD", cwd=gmd)
        if current != branch:
            if not keep_local:
                _run_git("fetch", "origin", "main", cwd=gmd)
            _run_git("checkout", "main", cwd=gmd)
        if keep_local:
            _strip_remotes(gmd)
        return

    if keep_local:
        # local-only pool: a git repo with no remote, never synced off-box
        _init_local_repo(branch, gmd)
        return

    gmd.parent.mkdir(parents=True, exist_ok=True)
    try:
        remote = memory_remote()
    except MemoryRemoteMissing:
        # No remote: local empty repo (bench / offline dev), no origin
        _init_local_repo(branch, gmd)
        return

    # `git clone` into a non-existent directory — cwd is the parent
    subprocess.run(  # noqa: S603
        ["git", "clone", remote, str(gmd)],
        check=True,
        creationflags=CREATE_NO_WINDOW,
    )

    # Whether the remote already has `main`
    remote_branches = _run_git("ls-remote", "--heads", "origin", branch, cwd=gmd)
    if remote_branches:
        _run_git("checkout", branch, cwd=gmd)
    else:
        # Fresh remote — push an initial main
        _run_git("checkout", "-b", branch, cwd=gmd)
        _run_git("push", "-u", "origin", branch, cwd=gmd)

    # Write .gitignore (if missing, commit + push)
    gitignore = gmd / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text(_DEFAULT_GITIGNORE)
        _run_git("add", ".gitignore", cwd=gmd)
        _run_git("commit", "-m", f"init: .gitignore on {branch}", cwd=gmd)
        _run_git("push", cwd=gmd)


def status() -> RepoStatus:
    """Inspect this host's agent-runner memory branch state.

    Raises:
        MemoryRepoUninitialized: `~/.ava/memory` is not yet a git
            repo (run `ava memory init`).
    """
    if not is_initialized():
        raise MemoryRepoUninitialized(
            f"{memory_dir()} is not yet a git repo — run `ava memory init` first."
        )

    branch = _run_git("rev-parse", "--abbrev-ref", "HEAD")
    is_clean = _run_git("status", "--porcelain") == ""
    local_commits = int(_run_git("rev-list", "--count", "HEAD"))

    # ahead/behind origin/main — origin/main may not exist (fresh
    # remote / only machine branch pushed); then 0/0 (no reference
    # frame, not an error).
    ahead = behind = 0
    with contextlib.suppress(subprocess.CalledProcessError):
        from shared.config import settings

        counts = _run_git(
            "rev-list", "--left-right", "--count", f"HEAD...origin/{settings.general.track_branch}"
        )
        ahead_s, behind_s = counts.split()
        ahead, behind = int(ahead_s), int(behind_s)

    fetch_head = memory_dir() / ".git" / "FETCH_HEAD"
    if fetch_head.exists():
        last_fetch = (
            datetime.datetime.fromtimestamp(
                fetch_head.stat().st_mtime,
                tz=datetime.UTC,
            )
            .astimezone()
            .isoformat(timespec="seconds")
        )
    else:
        last_fetch = "never"

    return RepoStatus(
        branch=branch,
        is_clean=is_clean,
        local_commits_on_branch=local_commits,
        ahead_of_main=ahead,
        behind_main=behind,
        last_fetch=last_fetch,
    )
