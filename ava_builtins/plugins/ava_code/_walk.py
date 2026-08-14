"""Walk up the path collecting context files. See `find_context_files_along_path` docstring."""

import subprocess
from pathlib import Path

from shared.log import logger
from shared.platform import CREATE_NO_WINDOW

# Cache only *successful* git-root resolutions, keyed by resolved dir path. A
# dir's repo root is stable once the repo exists, so caching it avoids repeated
# ~10-30ms forks on high-frequency `ava.files.read`. A negative result ("not a
# repo") is deliberately NOT cached: it can flip during a session (`git init`,
# or a clone landing in a watched dir), and a stale None would hide that repo's
# AGENTS.md / project-local skills until restart.
_GIT_ROOT_CACHE: dict[str, Path] = {}


def _resolve_git_root(cwd: Path) -> Path | None:
    if not cwd.exists():
        return None
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
            creationflags=CREATE_NO_WINDOW,
            timeout=2,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        # git not installed / timeout (slow NFS) — not "not a git repo";
        # log it so ops can spot env issues like a broken $PATH. Plugin
        # behavior matches no-repo (return None = no walk on this path).
        logger.warning("[ava_code] git rev-parse failed cwd={cwd}: {e}", cwd=cwd, e=e)
        return None
    if result.returncode == 0:
        return Path(result.stdout.strip()).resolve()
    if result.returncode == 128:
        return None  # "not a git repository" — expected path
    # Other returncode (perm denied, other env issues) — log it
    logger.warning(
        "[ava_code] git rev-parse returncode={rc} cwd={cwd} stderr={err!r}",
        rc=result.returncode,
        cwd=cwd,
        err=result.stderr.strip(),
    )
    return None


def _git_root(start: Path) -> Path | None:
    """`git rev-parse --show-toplevel` to get the git repo root containing `start`.

    Not a git repo → None (expected, not logged; not cached — see _GIT_ROOT_CACHE).
    git not installed / timeout / other returncode → None + logger.warning
    (distinguishes env issues from not-a-repo so a user with a broken $PATH
    can pinpoint the problem).
    """
    cwd = start if start.is_dir() else start.parent
    key = str(cwd.resolve())
    cached = _GIT_ROOT_CACHE.get(key)
    if cached is not None:
        return cached
    root = _resolve_git_root(cwd)
    if root is not None:
        _GIT_ROOT_CACHE[key] = root
    return root


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
