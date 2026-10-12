"""What counts as a direct entry of a directory for the 20-entry structure budget."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import Path, PurePosixPath


def tracked_children(listing: str) -> dict[str, set[str]]:
    """Every directory's direct children from Git's NUL-delimited tracked paths."""
    if listing and not listing.endswith("\0"):
        raise ValueError("tracked Git paths are not NUL-terminated")
    children: dict[str, set[str]] = {}
    for name in listing.split("\0")[:-1]:
        path = PurePosixPath(name)
        if not path.parts or path.is_absolute() or ".." in path.parts or path.as_posix() != name:
            raise ValueError(f"invalid tracked Git path: {name!r}")
        for depth, child in enumerate(path.parts):
            directory = "/".join(path.parts[:depth])
            children.setdefault(directory, set()).add(child)
    return children


def selected_directories(
    children: Mapping[str, set[str]], targets: Iterable[Path], repo_root: Path
) -> set[str]:
    """Select tracked descendants and ancestors; the repository root alone has no cap."""
    selected: set[str] = set()
    for target in targets:
        try:
            relative = target.relative_to(repo_root).as_posix()
        except ValueError:
            continue
        if relative == ".":
            selected.update(children)
            continue
        selected.update(
            directory
            for directory in children
            if directory == relative or directory.startswith(f"{relative}/")
        )
        parts = PurePosixPath(relative).parts
        selected.update("/".join(parts[:depth]) for depth in range(1, len(parts)))
    return selected.intersection(children).difference({""})


def entries(directory: Path) -> list[Path]:
    """Candidates for the existing Python file-budget scope, without following links."""
    return [
        entry
        for entry in directory.iterdir()
        if not entry.is_symlink()
        and not entry.name.startswith(".")
        and entry.name != "__pycache__"
        and not (entry.name == "migrations" and entry.is_dir())
    ]


def selected_under(target: Path, scope: Path, repo_root: Path) -> Path | None:
    """An existing Python file-budget target within its scope, without following links."""
    if target == scope or scope in target.parents:
        selected = target
    elif target in scope.parents:
        selected = scope
    else:
        return None
    relative = selected.relative_to(repo_root)
    if any(
        part.startswith(".") or part in {"__pycache__", "migrations"} for part in relative.parts
    ) or any(path.is_symlink() for path in (selected, *selected.parents)):
        return None
    return selected
