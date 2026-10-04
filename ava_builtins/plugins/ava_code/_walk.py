"""Walk up the path collecting context files. See `find_context_files_along_path` docstring."""

from pathlib import Path


def _is_git_root(directory: Path) -> bool:
    """Whether `directory` holds a git repository or worktree: a `.git` file (linked worktree,
    submodule) or a `.git` directory with a `HEAD`. An empty or foreign `.git` is skipped, as git
    itself keeps looking upward past it."""
    dotgit = directory / ".git"
    return dotgit.is_file() or (dotgit / "HEAD").exists()


def _git_root(start: Path) -> Path | None:
    """The nearest ancestor of `start` (itself included, when a directory) that is a git
    repository root, or None. Resolved by walking up for `.git`, so nothing is cached and a repo
    created mid-session (`git init`, a clone landing in a watched dir) surfaces immediately."""
    cwd = (start if start.is_dir() else start.parent).resolve()
    return next((d for d in (cwd, *cwd.parents) if _is_git_root(d)), None)


def project_skill_roots(cwd: Path) -> list[Path]:
    """Project-local skill folders for the repo containing `cwd`.

    Returns the git root's `.claude/skills` (Claude Code compatibility),
    `.agents/skills` (the open Agent Skills standard directory) and `.ava/skills`
    (Ava's own repo-local skills), in that order. They are scanned in list order
    with last-wins, so `.ava/skills` takes precedence on a name collision — an
    Ava-native skill overrides a same-named compat one, and the Ava-native path
    keeps working after a repo moves its skills to `.agents/skills` and turns
    `.ava/skills` into a link back to it. Only existing directories are
    returned. `cwd` not under a git repo → [].
    """
    root = _git_root(cwd)
    if root is None:
        return []
    candidates = (
        root / ".claude" / "skills",
        root / ".agents" / "skills",
        root / ".ava" / "skills",
    )
    return [d for d in candidates if d.is_dir()]


# Context files surfaced to the agent, in per-directory scan order. AGENTS.md
# (Ava's / the cross-tool standard) before CLAUDE.md (Claude Code's native).
_CONTEXT_FILENAMES = ("AGENTS.md", "CLAUDE.md")


def find_context_files_along_path(target: Path) -> list[Path]:
    """Walk up from `target` collecting context files, ordered "farthest → nearest".

    Context files are AGENTS.md and CLAUDE.md (see `_CONTEXT_FILENAMES`). Both
    are collected at every directory level; within a level AGENTS.md comes
    before CLAUDE.md.

    Boundary = the ancestor on the path **farthest** from target (fewest parts
    = shallowest depth) among {git_root, $HOME}. Neither on the path → return
    [] (no walk, no-op).

    Example: target=/Users/me/proj/sub/foo.py, git_root=/Users/me/proj, HOME=/Users/me
        Both {/Users/me/proj, /Users/me} are ancestors on the path;
        $HOME is shallower (fewer parts) → boundary = $HOME
        walk: me → proj → sub, collecting context files at each level
        returns the AGENTS.md/CLAUDE.md present in /Users/me, then
        /Users/me/proj, then /Users/me/proj/sub — so the agent sees global →
        local conventions.

    Args:
        target: Starting point (file or dir). For a file, walk starts at parent dir.

    Returns:
        Resolved context-file path list, farthest first, nearest last. Levels
        where a context file is a directory or does not exist are skipped.
    """
    target = target.resolve()
    start = target if target.is_dir() else target.parent
    if not start.exists():
        return []

    home = Path.home().resolve()
    git_root = _git_root(start)

    # Ancestor candidates on the path: start itself or its ancestors
    ancestors = [start, *start.parents]
    boundaries: list[Path] = []
    if home in ancestors:
        boundaries.append(home)
    if git_root and git_root in ancestors:
        boundaries.append(git_root)

    if not boundaries:
        return []  # Not under a git repo or $HOME: don't walk

    # Farthest boundary = fewest parts = shallowest depth = ancestor furthest
    # from target (on the path).
    boundary = min(boundaries, key=lambda p: len(p.parts))

    # Directories from start up to (and including) the boundary, then reversed
    # to farthest→nearest so the agent sees global conventions before local.
    dirs: list[Path] = []
    current = start
    while True:
        dirs.append(current)
        if current == boundary:
            break
        if current.parent == current:  # fs root fallback, only reached if invariant breaks
            break
        current = current.parent
    dirs.reverse()

    collected: list[Path] = []
    seen: set[Path] = set()
    for d in dirs:
        for name in _CONTEXT_FILENAMES:
            f = d / name
            if f.is_file():  # is_file: also excludes dir / nonexistent
                real = f.resolve()
                if real not in seen:
                    collected.append(real)
                    seen.add(real)
    return collected
