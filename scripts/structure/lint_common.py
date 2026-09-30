"""Shared scan scope, file traversal and text decoding for standalone repository lints."""

from __future__ import annotations

import re
import subprocess
from collections.abc import Iterable
from pathlib import Path

# The framework code: every production Python package at the repo root. Lints
# whose scope is "the framework" import this instead of keeping their own copy,
# so a new or renamed package cannot drift out of one lint's scope unnoticed.
# `shared` is the time-boxed release-probe shell (see shared/__init__.py); it ships
# in the wheel, so it stays in this list until it retires.
FRAMEWORK_DIRS = (
    "agent",
    "ava",
    "ava_builtins",
    "base",
    "cli",
    "gateway",
    "ops",
    "services",
    "shared",
)


# A test lives in a `tests/` directory: the top-level `tests/` (e2e, contract
# and shared-support tests) or a package's own `<pkg>/**/tests/`. Every lint that
# exempts tests, or scans them on purpose, decides it with this one pattern.
TEST_DIR = re.compile(r"(^|/)tests?/")


def is_test_path(rel_path: str) -> bool:
    """Whether a repo-relative posix path sits inside any `tests/` directory."""
    return TEST_DIR.search(rel_path) is not None


def is_repo_test_file(path: Path, repo_root: Path) -> bool:
    """`is_test_path` for an absolute path; a path outside the repo is never a test."""
    try:
        return is_test_path(path.relative_to(repo_root).as_posix())
    except ValueError:
        return False


def scan_roots(repo_root: Path, dirs: Iterable[str]) -> list[Path]:
    """Resolve a lint's configured scan dirs; a missing one is a configuration error.

    A lint whose scope names a directory that does not exist checks nothing
    there and still exits 0, so the gap stays invisible. Raise instead.
    """
    roots = [repo_root / d for d in dirs]
    missing = [root.relative_to(repo_root).as_posix() for root in roots if not root.is_dir()]
    if missing:
        raise FileNotFoundError(
            f"lint scan dir(s) not found under {repo_root}: {', '.join(missing)}"
        )
    return roots


def tracked_files(repo_root: Path) -> list[str]:
    """Return git-tracked paths in git's order, relative to the repo root."""
    # Fixed command line; the root comes from the calling script's own __file__.
    out = subprocess.run(  # noqa: S603 - fixed argv, script-derived repo root
        ["git", "-C", str(repo_root), "ls-files"],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    return [path for path in out.stdout.splitlines() if path]


def _rel_or_abs(path: Path, repo_root: Path) -> str:
    """Use a repo-relative path, or an absolute path outside the repo."""
    try:
        return path.relative_to(repo_root).as_posix()
    except ValueError:
        return path.as_posix()


def _resolved_target(argument: str, repo_root: Path) -> Path:
    path = Path(argument)
    if not path.is_absolute():
        candidate = repo_root / path
        if candidate.exists():
            path = candidate
    return path.resolve()


def _files_in_target(target: Path, repo_root: Path) -> list[str]:
    if target.is_file():
        return [_rel_or_abs(target, repo_root)]
    if target.is_dir():
        return [_rel_or_abs(path, repo_root) for path in target.rglob("*") if path.is_file()]
    return []


def resolve_targets(argv: list[str], repo_root: Path) -> tuple[list[str], list[str]]:
    """Return sorted explicit files and any original arguments that are missing."""
    targets = [_resolved_target(argument, repo_root) for argument in argv]
    missing = [a for a, target in zip(argv, targets, strict=True) if not target.exists()]
    if missing:
        return [], missing

    files = [file for target in targets for file in _files_in_target(target, repo_root)]
    return sorted(set(files)), []


def read_utf8_text(path: Path) -> str | None:
    """Skip unreadable, binary, and non-UTF-8 files."""
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if b"\x00" in data[:8192]:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def format_violation(rel_path: str, lineno: int, detail: str, content: str) -> str:
    """Format one stable file:line result; each lint supplies its own detail."""
    return f"{rel_path}:{lineno}: {detail} | {content}"
