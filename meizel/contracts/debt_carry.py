"""`debt carry`: recorded debt follows a file that git sees as renamed.

A baseline names files, so moving a file leaves its entries behind (paid, but not removed) and the
violations at the new place unrecorded. Carrying relocates the entries; it never adds one, never
raises a total, and records nothing that was not already recorded.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from meizel.contracts.debt import DebtTable, file_key, total
from meizel.contracts.debt_edit import write_baseline
from meizel.contracts.debt_report import read_baselines
from meizel.contracts.rule import project
from meizel.graph.model import Code, Fact, FactKind, MeizelError
from meizel.graph.universe import MANIFEST, Universe, load_universe, parent_of

__all__ = ["carry", "renames_since"]

_SEPARATOR = " -> "
_EDGE_CODES = frozenset(
    {
        Code.UNDECLARED_EDGE,
        Code.NOT_EXPORTED,
        Code.PRIVATE_MODULE,
        Code.PRIVATE_NAME,
        Code.CYCLE_EDGE,
    }
)


def _join(owner: str, path: str) -> str:
    return f"{owner}/{path}" if owner else path


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(  # noqa: S603 - fixed argv
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        raise MeizelError(f"git {args[0]} failed: {result.stderr.strip()}")
    return result.stdout


def default_base(root: Path) -> str:
    """The branch point with origin/main, or HEAD when there is no origin/main."""
    result = subprocess.run(  # noqa: S603 - fixed argv
        ["git", "-C", str(root), "merge-base", "HEAD", "origin/main"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else "HEAD"


def renames_since(root: Path, base: str) -> dict[str, str]:
    """`old -> new` for the files git detects as renamed between `base` and the working tree."""
    if base.startswith("-"):
        raise MeizelError(f"{base}: not a git ref")
    listing = _git(root, "diff", "-M", "--name-status", "-z", base).split("\0")
    found: dict[str, str] = {}
    index = 0
    while index < len(listing) and listing[index]:
        status = listing[index]
        if status.startswith("R"):
            old, new = listing[index + 1], listing[index + 2]
            if old.rsplit("/", 1)[-1] != MANIFEST and new.rsplit("/", 1)[-1] != MANIFEST:
                found[old] = new
            index += 3
        else:
            index += 2
    return found


def _owner_of_edge(src: str, dst: str) -> str:
    projection = project(Fact(src, dst, 0, FactKind.IMPORT))
    return projection.lca if projection is not None else parent_of(src)


def _relocated(
    owner: str, code: Code, key: str, moved: dict[str, str], gone: set[str]
) -> tuple[str, str] | None:
    """The `(owner, key)` an entry has after the renames, or None when it is not affected."""
    if code in _EDGE_CODES:
        head, _, name = key.partition("#")
        src, _, dst = head.partition(_SEPARATOR)
        old_src, old_dst = _join(owner, src), _join(owner, dst)
        if old_src not in gone and old_dst not in gone:
            return None
        new_src, new_dst = moved.get(old_src, old_src), moved.get(old_dst, old_dst)
        new_owner = _owner_of_edge(new_src, new_dst)
        within = len(new_owner) + 1 if new_owner else 0
        edge = f"{new_src[within:]}{_SEPARATOR}{new_dst[within:]}"
        return new_owner, f"{edge}#{name}" if name else edge
    file, _, what = key.partition("#")
    old = _join(owner, file)
    if old not in gone:
        return None
    new = moved[old]
    return parent_of(new), file_key(parent_of(new), new, what)


_PATCH_CODES = frozenset(
    {
        Code.FIRST_PARTY_PATCH,
        Code.STDLIB_PATCH,
        Code.THIRD_PARTY_PATCH,
        Code.ENV_PATCH,
        Code.UNRESOLVED_PATCH,
    }
)


def parse_renames(specs: list[str]) -> list[tuple[str, str]]:
    """`OLD=NEW` target renames, each side a non-empty dotted name."""
    renames: list[tuple[str, str]] = []
    for spec in specs:
        old, _, new = spec.partition("=")
        if not old or not new or old == new:
            raise MeizelError(
                f"{spec!r}: a target rename is written OLD=NEW with two different names"
            )
        renames.append((old, new))
    return renames


def _retargeted(key: str, renames: list[tuple[str, str]]) -> str | None:
    """The patch key with its target renamed (`file#form target`), or None when no rename applies."""
    file, _, what = key.partition("#")
    form, _, target = what.partition(" ")
    for old, new in renames:
        if target == old or target.startswith(f"{old}."):
            return f"{file}#{form} {new}{target[len(old) :]}"
    return None


def _plan(
    baselines: dict[str, DebtTable],
    moved: dict[str, str],
    universe: Universe,
    renames: list[tuple[str, str]],
) -> tuple[dict[str, DebtTable], list[str]]:
    gone = {old for old in moved if old not in universe.files}
    tables = {
        owner: {c: dict(rows) for c, rows in table.items()} for owner, table in baselines.items()
    }
    lines: list[str] = []
    for owner, table in baselines.items():
        for code, rows in table.items():
            for key, count in sorted(rows.items()):
                target = _relocated(owner, code, key, moved, gone)
                if target is None and code in _PATCH_CODES:
                    renamed = _retargeted(key, renames)
                    target = None if renamed is None else (owner, renamed)
                if target is None or target == (owner, key):
                    continue
                del tables[owner][code][key]
                landing = tables.setdefault(target[0], {}).setdefault(code, {})
                landing[target[1]] = landing.get(target[1], 0) + count
                lines.append(f"{code} {_join(owner, key)} => {_join(*target)} ({count})")
    return tables, lines


def carry(
    root: Path, base: str | None = None, rename_targets: list[str] | None = None
) -> list[str]:
    """Move entries to where their renamed files, or renamed patch targets, now are.

    Returns a line per entry moved. `rename_targets` (`OLD=NEW`) rewrites the target of a patch
    entry in the same source file, one for one: a symbol that was renamed, not a file.
    """
    moved = renames_since(root, base or default_base(root))
    universe = load_universe(root)
    baselines = read_baselines(universe)
    tables, lines = _plan(baselines, moved, universe, parse_renames(rename_targets or []))
    before = sum(total(t) for t in baselines.values())
    after = sum(total(t) for t in tables.values())
    if after != before:
        raise MeizelError(f"carrying would change the recorded total ({before} -> {after})")
    for owner in sorted(tables):
        if tables[owner] != baselines.get(owner, {}):
            write_baseline(root, owner, {c: r for c, r in tables[owner].items() if r})
    return lines
