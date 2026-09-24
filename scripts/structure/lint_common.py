"""Shared file traversal and text decoding for standalone repository lints."""

from __future__ import annotations

import subprocess
from pathlib import Path


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
