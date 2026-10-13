"""Project-local skill folders: where a repository keeps skills its agents mount.

Used by the ava-code plugin (working-directory skills) and by the cluster-ops
skill view, so it lives below both.
"""

from pathlib import Path


def _is_git_root(directory: Path) -> bool:
    """Whether `directory` holds a git repository or worktree: a `.git` file (linked worktree,
    submodule) or a `.git` directory with a `HEAD`. An empty or foreign `.git` is skipped, as git
    itself keeps looking upward past it."""
    dotgit = directory / ".git"
    return dotgit.is_file() or (dotgit / "HEAD").exists()


def git_root(start: Path) -> Path | None:
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
    root = git_root(cwd)
    if root is None:
        return []
    candidates = (
        root / ".claude" / "skills",
        root / ".agents" / "skills",
        root / ".ava" / "skills",
    )
    return [d for d in candidates if d.is_dir()]
