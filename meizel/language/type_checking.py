"""Rule: no `if TYPE_CHECKING:` import folding, except for the files listed here."""

from __future__ import annotations

import ast
from pathlib import Path

from meizel import allowances

__all__ = ["violations"]

# Files allowed to use `if TYPE_CHECKING:` — a real circular import or a heavy optional dependency.
# Listed in meizel.policy.toml under [type-checking.files], each with a one-line reason.
_TYPE_CHECKING_ALLOWED: frozenset[str] = frozenset(
    allowances.load(Path(__file__).resolve().parents[2]).reasons("type-checking", "files")
)


def _lines(tree: ast.Module) -> list[int]:
    """Line numbers of `if TYPE_CHECKING:` / `if typing.TYPE_CHECKING:` blocks."""
    hits: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        test = node.test
        name = (
            test.id
            if isinstance(test, ast.Name)
            else test.attr
            if isinstance(test, ast.Attribute)
            else None
        )
        if name == "TYPE_CHECKING":
            hits.append(node.lineno)
    return hits


def violations(tree: ast.Module, rel_path: str) -> list[tuple[int, str]]:
    """`(line, message)` for every unlisted `if TYPE_CHECKING:` block of a governed module."""
    if rel_path in _TYPE_CHECKING_ALLOWED:
        return []
    return [
        (
            lineno,
            "`if TYPE_CHECKING:` is banned — import at top level. "
            "App deps are always on the runtime path and LangGraph/Pydantic "
            "introspect type hints at runtime (deferred imports NameError). "
            "Real circular-import / heavy-dep cases: refactor, or add this file "
            "to [type-checking.files] in meizel.policy.toml with a reason.",
        )
        for lineno in _lines(tree)
    ]
