"""Expand `base.packages.declared_inputs` domains into the checkout's concrete files and modules."""

from __future__ import annotations

import os
from collections.abc import Iterator
from functools import lru_cache
from pathlib import Path

from base.packages.declared_inputs import OUTSIDE_REPOSITORY, matches

from .bindings import module_name

_WILDCARDS = frozenset("*?[")


def files(root: Path, patterns: tuple[str, ...]) -> Iterator[str]:
    """Repository-relative files a ``declared_path`` domain names in this checkout.

    A pattern without wildcards is a literal resource and is kept even when absent.
    """
    for pattern in patterns:
        parts = pattern.split("/")
        prefix = _literal_prefix(parts)
        if len(prefix) == len(parts):
            yield pattern
            continue
        yield from (
            path for path in _tree(root, "/".join(prefix)) if matches(path.split("/"), parts)
        )


def modules(root: Path, patterns: tuple[str, ...]) -> Iterator[str]:
    """Dotted modules a ``declared_import``/``declared_spec`` domain names in this checkout.

    A pattern without wildcards is returned as written, so a missing first-party
    target stays visible to the caller's existence check.
    """
    for pattern in patterns:
        parts = pattern.split(".")
        prefix = _literal_prefix(parts)
        if len(prefix) == len(parts):
            yield pattern
            continue
        for path in _tree(root, "/".join(prefix)):
            if path.endswith(".py"):
                dotted = module_name(path)
                if matches(dotted.split("."), parts):
                    yield dotted


def _literal_prefix(parts: list[str]) -> list[str]:
    prefix: list[str] = []
    for part in parts:
        if _WILDCARDS.intersection(part):
            break
        prefix.append(part)
    return prefix


@lru_cache(maxsize=64)
def _tree(root: Path, prefix: str) -> tuple[str, ...]:
    start = root / prefix if prefix else root
    found: list[str] = []
    for current, dirs, names in os.walk(start):
        dirs[:] = sorted(d for d in dirs if d not in OUTSIDE_REPOSITORY)
        base = Path(current).relative_to(root)
        found.extend((base / name).as_posix() for name in sorted(names))
    return tuple(found)
