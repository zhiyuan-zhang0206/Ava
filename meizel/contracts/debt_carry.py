"""`debt carry`: recorded debt follows a file that git sees as renamed, and a symbol moved by hand.

A baseline names files, so moving a file leaves its entries behind (paid, but not removed) and the
violations at the new place unrecorded. Carrying relocates the entries; it never adds one, never
raises a total, and records nothing that was not already recorded.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from meizel.contracts.debt import PATCH_CODES, DebtTable, file_key, total
from meizel.contracts.debt_edit import write_baseline
from meizel.contracts.debt_report import read_baselines
from meizel.contracts.debt_symbol import (
    SymbolMove,
    defines,
    parse_symbol_moves,
    symbol_debt,
)
from meizel.contracts.manifest import load_manifests
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


_PATCH_CODES = frozenset(PATCH_CODES)


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


def _source_at(root: Path, base: str, path: str) -> bytes:
    return _git(root, "show", f"{base}:{path}").encode()


def _check_move(root: Path, base: str, universe: Universe, move: SymbolMove) -> tuple[bytes, bytes]:
    """The old file at the base and the new file now, after checking that this is a move."""
    if move.new not in universe.files:
        raise MeizelError(
            f"{move.new} is not a file of the repository (missing, or ignored by git)"
        )
    before, after = _source_at(root, base, move.old), (root / move.new).read_bytes()
    here = root / move.old
    if here.exists() and defines(move.old, here.read_bytes(), move.name):
        raise MeizelError(f"{move.old} still defines {move.name}: that is a copy, not a move")
    for path, source, where in (
        (move.old, before, f"{move.old} at {base}"),
        (move.new, after, move.new),
    ):
        if not defines(path, source, move.name):
            raise MeizelError(f"{where} defines no top-level function or class {move.name}")
    return before, after


def _swap(
    tables: dict[str, DebtTable],
    move: SymbolMove,
    before: dict[tuple[Code, str, str], int],
    after: dict[tuple[Code, str, str], int],
    renames: list[tuple[str, str]],
) -> list[str]:
    """Rekey, one for one, the entries the symbol had in the old file to its new file.

    At most what the symbol cost before, what it costs now and what is recorded moves, so a
    violation the symbol gained on the way stays unrecorded.
    """
    lines: list[str] = []
    for (code, owner, recorded), count in sorted(before.items()):
        key = (_retargeted(recorded, renames) if code in _PATCH_CODES else None) or recorded
        landing = _relocated(owner, code, key, {move.old: move.new}, {move.old})
        if landing is None:
            continue
        rows = tables.get(owner, {}).get(code, {})
        amount = min(count, after.get((code, *landing), 0), rows.get(key, 0))
        if amount == 0:
            continue
        rows[key] -= amount
        if not rows[key]:
            del rows[key]
        row = tables.setdefault(landing[0], {}).setdefault(code, {})
        row[landing[1]] = row.get(landing[1], 0) + amount
        lines.append(f"{code} {_join(owner, key)} => {_join(*landing)} ({amount}, {move.name})")
    return lines


def _move_symbols(
    root: Path,
    base: str,
    universe: Universe,
    tables: dict[str, DebtTable],
    moves: list[SymbolMove],
    renames: list[tuple[str, str]],
) -> list[str]:
    manifests, _ = load_manifests(universe)
    lines: list[str] = []
    for move in moves:
        old_source, new_source = _check_move(root, base, universe, move)
        before = symbol_debt(universe, manifests, move.old, old_source, move.name)
        after = symbol_debt(universe, manifests, move.new, new_source, move.name)
        lines.extend(_swap(tables, move, before, after, renames))
    return lines


def carry(
    root: Path,
    base: str | None = None,
    rename_targets: list[str] | None = None,
    move_symbols: list[str] | None = None,
) -> list[str]:
    """Move entries to where their renamed files, renamed patch targets or moved symbols now are.

    Returns a line per entry moved. `rename_targets` (`OLD=NEW`) rewrites the target of a patch
    entry in the same source file, one for one: a symbol that was renamed, not a file.
    `move_symbols` (`OLD_FILE::NAME=NEW_FILE`) rekeys the entries of a function or class that
    moved from one file to another, which git cannot see as a rename.
    """
    base = base or default_base(root)
    moved = renames_since(root, base)
    universe = load_universe(root)
    baselines = read_baselines(universe)
    renames = parse_renames(rename_targets or [])
    symbols = parse_symbol_moves(move_symbols or [])
    tables, lines = _plan(baselines, moved, universe, renames)
    lines.extend(_move_symbols(root, base, universe, tables, symbols, renames))
    before = sum(total(t) for t in baselines.values())
    after = sum(total(t) for t in tables.values())
    if after != before:
        raise MeizelError(f"carrying would change the recorded total ({before} -> {after})")
    for owner in sorted(tables):
        if tables[owner] != baselines.get(owner, {}):
            write_baseline(root, owner, {c: r for c, r in tables[owner].items() if r})
    return lines
