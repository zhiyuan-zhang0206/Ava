"""The 800-line file ceiling and the 20-entry directory cap."""

from __future__ import annotations

import subprocess
from pathlib import Path

from meizel.budgets import directory_budget

__all__ = ["DIRECTORY_CEILING", "HARD_CEILING", "budget_files", "check"]

HARD_CEILING = 800
DIRECTORY_CEILING = 20


def budget_files(
    targets: list[Path], repo_root: Path, structure_dirs: tuple[str, ...]
) -> set[Path]:
    """Collect the existing Python file-budget scope without following links."""
    files: set[Path] = set()
    visited: set[Path] = set()

    def visit(directory: Path) -> None:
        if directory in visited:
            return
        visited.add(directory)
        for entry in directory_budget.entries(directory):
            if entry.is_dir():
                visit(entry)
            elif entry.is_file() and entry.suffix == ".py":
                files.add(entry)

    for target in targets:
        for scope in (repo_root / name for name in structure_dirs):
            selected = directory_budget.selected_under(target, scope, repo_root)
            if selected is None:
                continue
            if selected.is_dir():
                visit(selected)
            elif selected.is_file() and selected.suffix == ".py":
                files.add(selected)
    return files


def check(
    targets: list[Path],
    directory_targets: list[Path],
    repo_root: Path,
    structure_dirs: tuple[str, ...],
) -> list[str]:
    """Violations of both ceilings; raises on an unreadable Git listing."""
    files = budget_files(targets, repo_root, structure_dirs)
    errors: list[str] = []
    for path in sorted(files):
        try:
            count = len(path.read_text(encoding="utf-8").splitlines())
        except (OSError, UnicodeDecodeError):
            continue  # Preserve the shared lint contract for unreadable members.
        name = path.relative_to(repo_root).as_posix()
        if count > HARD_CEILING:
            errors.append(
                f"{name}:{count}: file is {count} lines, over the {HARD_CEILING}-line hard ceiling — split it"
            )
    listing = subprocess.run(  # noqa: S603 — local git query, no shell
        ["git", "-C", str(repo_root), "ls-files", "-z"], capture_output=True, text=True, check=False
    )
    listing.check_returncode()
    children = directory_budget.tracked_children(listing.stdout)
    for name in sorted(
        directory_budget.selected_directories(children, directory_targets, repo_root)
    ):
        count = len(children[name])
        if count > DIRECTORY_CEILING:
            errors.append(
                f"{name}: directory has {count} direct entries, over the {DIRECTORY_CEILING}-entry cap — split it"
            )
    return errors
