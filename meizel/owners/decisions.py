"""Rule: a design decision has exactly one owning module; every other site is a bypass.

`DECISIONS` names the decisions. A listed exemption must stay current: it fails
when the module no longer bypasses the owner, or no longer exists.
"""

from __future__ import annotations

import ast
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from meizel import allowances

__all__ = [
    "DECISIONS",
    "Decision",
    "Sites",
    "allowlist_errors",
    "missing_allowlist_errors",
    "owner_bypasses",
]

Sites = dict[str, list[int]]

_POLICY = allowances.load(Path(__file__).resolve().parents[2])


@dataclass
class _DialBindings:
    """Local names bound to psycopg / psycopg_pool entry points in one module."""

    psycopg: set[str] = field(default_factory=set[str])
    connect: set[str] = field(default_factory=set[str])
    connection: set[str] = field(default_factory=set[str])
    pool_module: set[str] = field(default_factory=set[str])
    pool: set[str] = field(default_factory=set[str])

    def bind_import(self, node: ast.Import) -> None:
        for alias in node.names:
            if alias.name == "psycopg":
                self.psycopg.add(alias.asname or alias.name)
            elif alias.name == "psycopg_pool":
                self.pool_module.add(alias.asname or alias.name)

    def bind_from(self, node: ast.ImportFrom, roots: tuple[str, ...]) -> None:
        module = node.module or ""
        governed = node.level > 0 or module.split(".")[0] in roots
        for alias in node.names:
            name = alias.asname or alias.name
            if module == "psycopg" and alias.name == "connect":
                self.connect.add(name)
            elif module == "psycopg" and alias.name in {"Connection", "AsyncConnection"}:
                self.connection.add(name)
            elif (module == "psycopg_pool" or governed) and alias.name.endswith("ConnectionPool"):
                # psycopg_pool's pools, or this repo's own pool subclasses.
                self.pool.add(name)

    def is_pool_class(self, node: ast.expr) -> bool:
        node = _unsubscript(node)
        if isinstance(node, ast.Name):
            return node.id in self.pool
        return (
            isinstance(node, ast.Attribute)
            and node.attr.endswith("ConnectionPool")
            and isinstance(node.value, ast.Name)
            and node.value.id in self.pool_module
        )

    def is_dial(self, call: ast.Call) -> bool:
        func = _unsubscript(call.func)
        if isinstance(func, ast.Name):
            return func.id in self.connect or func.id in self.pool
        if not isinstance(func, ast.Attribute) or func.attr != "connect":
            return self.is_pool_class(func)
        target = _unsubscript(func.value)
        if isinstance(target, ast.Name):
            return target.id in self.psycopg or target.id in self.connection
        return (
            isinstance(target, ast.Attribute)
            and target.attr in {"Connection", "AsyncConnection"}
            and isinstance(target.value, ast.Name)
            and target.value.id in self.psycopg
        )


def _unsubscript(node: ast.expr) -> ast.expr:
    return node.value if isinstance(node, ast.Subscript) else node


def _postgres_dials(tree: ast.Module, roots: tuple[str, ...]) -> list[int]:
    """Lines that open a Postgres transport: psycopg connects and pool constructions."""
    bindings = _DialBindings()
    calls: list[ast.Call] = []
    classes: list[ast.ClassDef] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            calls.append(node)
        elif isinstance(node, ast.ClassDef):
            classes.append(node)
        elif isinstance(node, ast.Import):
            bindings.bind_import(node)
        elif isinstance(node, ast.ImportFrom):
            bindings.bind_from(node, roots)
    for cls in classes:  # a pool subclass defined here is constructed like its base
        if any(bindings.is_pool_class(base) for base in cls.bases):
            bindings.pool.add(cls.name)
    return sorted(call.lineno for call in calls if bindings.is_dial(call))


@dataclass(frozen=True)
class Decision:
    """A design decision with exactly one owning module; other sites are bypasses."""

    owners: frozenset[str]
    find: Callable[[ast.Module, tuple[str, ...]], list[int]]
    fix: str
    # path -> one-line reason the site genuinely cannot go through the owner; the entries are
    # listed in meizel.policy.toml under [single-owner.<decision>].
    allowed: dict[str, str] = field(default_factory=dict[str, str])


DECISIONS: dict[str, Decision] = {
    "postgres-dial": Decision(
        owners=frozenset({"base/db/connections.py"}),
        find=_postgres_dials,
        fix=(
            "dial through base.db.connect() / base.db.pool() (the cluster's own URL) "
            "or base.db.connect_url() (an explicit target the caller names), which own "
            "the transport posture (prepare_threshold=None, keepalives, statement ceiling "
            "or unbounded, sslmode, pooled-session scrub)"
        ),
        allowed=_POLICY.reasons("single-owner", "postgres-dial"),
    ),
}


def owner_bypasses(tree: ast.Module, rel_path: str, roots: tuple[str, ...]) -> Sites:
    sites: Sites = {}
    for name, decision in DECISIONS.items():
        if rel_path in decision.owners or rel_path in decision.allowed:
            continue
        lines = decision.find(tree, roots)
        if lines:
            sites[f"{rel_path}::{name}"] = lines
    return sites


def allowlist_errors(
    tree: ast.Module, rel_path: str, roots: tuple[str, ...]
) -> list[tuple[int, str]]:
    """A listed exemption whose module no longer bypasses the owner is stale."""
    return [
        (1, f"stale {name} allowlist entry — the module no longer bypasses the owner; {_UNLIST}")
        for name, decision in DECISIONS.items()
        if rel_path in decision.allowed and not decision.find(tree, roots)
    ]


_UNLIST = f"remove it from [single-owner.<decision>] in {allowances.POLICY}"


def missing_allowlist_errors(repo_root: Path) -> list[str]:
    """A listed exemption for a module that no longer exists is stale too."""
    return [
        f"{path}:1: stale {name} allowlist entry — the module no longer exists; {_UNLIST}"
        for name, decision in DECISIONS.items()
        for path in sorted(decision.allowed)
        if not (repo_root / path).is_file()
    ]
