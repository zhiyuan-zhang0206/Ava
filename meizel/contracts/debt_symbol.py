"""`debt carry --move-symbol`: what one function or class costs, so its debt can follow it.

A baseline names files, not functions, so a fixture that moves into another file leaves its entries
in the old file's keys. The move is read from the code on both sides: the violations inside the
symbol's lines in the old file at the base, and inside the same symbol in the new file.
"""

from __future__ import annotations

import ast
from collections import Counter
from dataclasses import dataclass

from meizel.contracts.analysis import diagnose
from meizel.contracts.manifest import Manifest
from meizel.graph.model import Code, MeizelError
from meizel.graph.universe import Universe
from meizel.language.scan import scan_source

__all__ = ["SymbolMove", "defines", "parse_symbol_moves", "symbol_debt"]

type Found = dict[tuple[Code, str, str], int]


@dataclass(frozen=True, slots=True)
class SymbolMove:
    """The top-level function or class `name` moved from file `old` to file `new`."""

    old: str
    name: str
    new: str


def parse_symbol_moves(specs: list[str]) -> list[SymbolMove]:
    """`OLD_FILE::NAME=NEW_FILE` moves, each side a Python file and NAME a plain identifier."""
    moves: list[SymbolMove] = []
    for spec in specs:
        head, _, new = spec.partition("=")
        old, _, name = head.partition("::")
        if not (old.endswith(".py") and new.endswith(".py") and name.isidentifier()) or old == new:
            raise MeizelError(
                f"{spec!r}: a symbol move is written OLD_FILE::NAME=NEW_FILE with two different "
                "Python files and the name of a top-level function or class"
            )
        moves.append(SymbolMove(old, name, new))
    return moves


def _span(tree: ast.Module, name: str) -> tuple[int, int] | None:
    """The first and last line of the top-level function or class `name`, decorators included."""
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) and (
            node.name == name
        ):
            first = min([node.lineno, *(d.lineno for d in node.decorator_list)])
            return first, node.end_lineno or node.lineno
    return None


def _parse(path: str, source: bytes) -> ast.Module:
    try:
        return ast.parse(source, filename=path)
    except (SyntaxError, ValueError) as error:
        raise MeizelError(f"{path}: cannot parse: {error}") from error


def defines(path: str, source: bytes, name: str) -> bool:
    """Whether the source of `path` defines the top-level function or class `name`."""
    return _span(_parse(path, source), name) is not None


def symbol_debt(
    universe: Universe,
    manifests: dict[str, Manifest],
    path: str,
    source: bytes,
    name: str,
) -> Found:
    """The violations inside `name` in this version of `path`, counted per `(code, owner, key)`."""
    span = _span(_parse(path, source), name)
    if span is None:
        raise MeizelError(f"{path} defines no top-level function or class {name}")
    found: Counter[tuple[Code, str, str]] = Counter()
    for item in diagnose(scan_source(universe, path, source), manifests):
        if item.debt is not None and span[0] <= item.line <= span[1]:
            found[(item.code, item.debt.owner, item.debt.key)] += 1
    return dict(found)
