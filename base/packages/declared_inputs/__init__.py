"""Runtime-chosen imports and repository reads with a statically declared domain.

PR test selection proves which repository files a test can reach without running
code (`scripts/ci/test_impact.py`). A module name or path computed at runtime is
opaque to that proof. These doors carry a literal ``within`` domain: the static
analysis expands it into dependency edges, and the door rejects any repository
target outside it, so the declaration cannot drift from what the code loads.

Only repository targets are constrained. A module whose top-level package is not
a checkout directory (an installed dependency, a synthetic ``sys.modules`` entry)
and a path outside the checkout (``$AVA_HOME``, a temporary directory) pass through.
Patterns match one segment per ``*``-style glob and any number of segments per
``**``: dotted segments for modules, ``/`` segments for repository-relative paths.
"""

from __future__ import annotations

import importlib
import importlib.util
from collections.abc import Sequence
from fnmatch import fnmatchcase
from importlib.machinery import ModuleSpec
from os import PathLike
from pathlib import Path
from types import ModuleType

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
# Checkout directories that hold tooling or caches, never repository inputs.
OUTSIDE_REPOSITORY = frozenset({".git", ".venv", "__pycache__", "node_modules"})


def matches(parts: Sequence[str], pattern: Sequence[str]) -> bool:
    """Whether ``parts`` matches ``pattern`` segment by segment; ``**`` spans any count."""
    if not pattern:
        return not parts
    head, rest = pattern[0], pattern[1:]
    if head == "**":
        return any(matches(parts[offset:], rest) for offset in range(len(parts) + 1))
    return bool(parts) and fnmatchcase(parts[0], head) and matches(parts[1:], rest)


def declared_import(module: str, *, within: tuple[str, ...]) -> ModuleType:
    """Import ``module`` after checking a repository module against its declared domain."""
    _check_module(module, within)
    return importlib.import_module(module)


def declared_spec(module: str, *, within: tuple[str, ...]) -> ModuleSpec | None:
    """``importlib.util.find_spec`` for an existence probe with a declared domain."""
    _check_module(module, within)
    return importlib.util.find_spec(module)


def declared_path(path: str | PathLike[str], *, within: tuple[str, ...] = ()) -> Path:
    """Return ``path`` unchanged after checking a repository file against its domain.

    An empty ``within`` declares that the path is never a repository input, such
    as runtime state under ``$AVA_HOME`` or a temporary file.
    """
    candidate = Path(path)
    resolved = candidate.resolve()
    if not resolved.is_relative_to(REPOSITORY_ROOT):
        return candidate
    parts = resolved.relative_to(REPOSITORY_ROOT).parts
    if not parts or OUTSIDE_REPOSITORY.intersection(parts):
        return candidate
    if not any(matches(parts, pattern.split("/")) for pattern in within):
        raise ValueError(f"{candidate} is a repository path outside its declared domain {within!r}")
    return candidate


def _check_module(module: str, within: tuple[str, ...]) -> None:
    top = module.split(".", maxsplit=1)[0]
    repository = top not in OUTSIDE_REPOSITORY and (
        (REPOSITORY_ROOT / top).is_dir() or (REPOSITORY_ROOT / f"{top}.py").is_file()
    )
    if repository and not any(matches(module.split("."), pattern.split(".")) for pattern in within):
        raise ValueError(
            f"{module!r} is a repository module outside its declared domain {within!r}"
        )
