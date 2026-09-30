"""What counts as a direct entry of a directory for the 20-entry structure budget."""

from __future__ import annotations

from pathlib import Path


def entries(directory: Path) -> list[Path]:
    """The members of `directory` the budget looks at: no links, hidden entries,
    `__pycache__` or migrations subtrees."""
    return [
        entry
        for entry in directory.iterdir()
        if not entry.is_symlink()
        and not entry.name.startswith(".")
        and entry.name != "__pycache__"
        and not (entry.name == "migrations" and entry.is_dir())
    ]


def is_tests_layer(directory: Path) -> bool:
    """A `tests/` directory without `__init__.py`: the test files beside a package's code
    (or the top-level `tests/`), flat by nature. With `__init__.py` it is a real Python
    package and is budgeted like any other."""
    return directory.name == "tests" and not (directory / "__init__.py").exists()


def counts_toward_budget(entry: Path) -> bool:
    """A .py/.pyi file, or a subdirectory with content. A directory holding
    nothing but `__pycache__` / hidden files (left behind locally when a package
    is renamed or removed) or nothing at all is not a tree CI checks out, so it
    never counts. Neither does a `docs/` layer (the package's OKF documentation)
    or a `tests/` layer without `__init__.py`: neither is code structure (a `docs`
    directory with `__init__.py` is a real Python package and counts)."""
    if entry.is_dir():
        if entry.name == "docs" and not (entry / "__init__.py").exists():
            return False
        return not is_tests_layer(entry) and bool(entries(entry))
    return entry.is_file() and entry.suffix in {".py", ".pyi"}
