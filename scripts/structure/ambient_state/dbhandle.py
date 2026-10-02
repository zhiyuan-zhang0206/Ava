"""The ambient-db rule: a package that holds a `Database` handle dials no database ambiently.

`base.db.connect()` / `pool()` / `async_pool()` / `direct_db_url()` and a `write_transaction()`
without a pool build the dial from the live settings at each call: a process-default
database that nothing passes in. A package listed in `DB_HANDLE_PACKAGES` takes a
`Database` (or an open pool/connection) from its composition root instead; any of those
ambient entries in one of its non-test modules is a site, frozen like the other
ambient-state sites as `path::ambient-db:<name>`, and a `Database.from_settings()` call
outside the roots named for the package is one too. When the last package is listed the
shim itself is deleted.
"""

from __future__ import annotations

import ast

from scripts.structure.ambient_state import allowlist as allow
from scripts.structure.ambient_state.scan import Hit

AMBIENT_DB = "ambient-db"
FIX = (
    "take a `Database` (or its pool/connection) from the composition root named in "
    "DB_HANDLE_PACKAGES instead of dialing from the live settings; only the root calls "
    "`Database.from_settings()`"
)
_PRIMITIVES = frozenset({"connect", "pool", "async_pool", "direct_db_url"})
_DB_MODULES = frozenset({"base.db", "base.db.connections"})
_TRANSACTION_MODULES = frozenset({"base.db", "base.db.transaction"})


def package_of(rel: str) -> str | None:
    return next((pkg for pkg in allow.DB_HANDLE_PACKAGES if rel.startswith(f"{pkg}/")), None)


class _Names:
    """What the module binds from `base.db`: module aliases and imported entry points."""

    def __init__(self, tree: ast.Module) -> None:
        self.modules: set[str] = {"base.db", "base.db.connections"}
        self.primitives: dict[str, str] = {}
        self.write_transactions: set[str] = set()
        self.databases: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name in _DB_MODULES and alias.asname:
                        self.modules.add(alias.asname)
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                self._from_import(node)

    def _from_import(self, node: ast.ImportFrom) -> None:
        for alias in node.names:
            local = alias.asname or alias.name
            if node.module == "base" and alias.name == "db":
                self.modules.add(local)
            elif node.module in _DB_MODULES and alias.name in _PRIMITIVES:
                self.primitives[local] = alias.name
            elif node.module in _TRANSACTION_MODULES and alias.name == "write_transaction":
                self.write_transactions.add(local)
            elif node.module in {"base.db", "base.db.handle"} and alias.name == "Database":
                self.databases.add(local)


def _ambient_write_transaction(call: ast.Call) -> bool:
    return not call.args and all(kw.arg != "pool" for kw in call.keywords)


def _call_hit(call: ast.Call, names: _Names, *, is_root: bool) -> str | None:
    func = call.func
    if isinstance(func, ast.Attribute) and isinstance(func.value, (ast.Name, ast.Attribute)):
        owner = ast.unparse(func.value)
        if owner in names.modules:
            if func.attr in _PRIMITIVES:
                return func.attr
            if func.attr == "write_transaction" and _ambient_write_transaction(call):
                return "write_transaction"
        if owner in names.databases and func.attr == "from_settings" and not is_root:
            return "Database.from_settings"
    elif isinstance(func, ast.Name):
        if func.id in names.primitives:
            return names.primitives[func.id]
        if func.id in names.write_transactions and _ambient_write_transaction(call):
            return "write_transaction"
    return None


def hits(tree: ast.Module, rel: str) -> list[Hit]:
    """Every ambient database dial in a governed module."""
    package = package_of(rel)
    if package is None:
        return []
    names = _Names(tree)
    is_root = rel in allow.DB_HANDLE_PACKAGES[package]
    found: list[Hit] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and (name := _call_hit(node, names, is_root=is_root)) is not None
        ):
            found.append(Hit(AMBIENT_DB, name, node.lineno))
    return found
