"""Locality rules for the structure gate: package doors and single decision owners.

Both rules measure `path::target -> [line, ...]` sites per module; the frozen
counts live in the `private_imports` / `owner_bypasses` sections of
scripts/structure/baseline.json, and the rules themselves are documented in the
scripts/lint_code_structure.py header (Rules 4 and 5).
"""

from __future__ import annotations

import ast
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

SECTIONS = ("private_imports", "owner_bypasses")
_BASELINE_PATH = "scripts/structure/baseline.json"
_TEST_PATTERNS = (
    re.compile(r"(^|/)tests?/"),
    re.compile(r"(^|/)test_[^/]+\.py$"),
    re.compile(r"_test\.py$"),
)

Sites = dict[str, list[int]]


def _is_private(part: str) -> bool:
    return part.startswith("_") and not part.startswith("__")


def _package_of(rel_path: str) -> list[str]:
    """Dotted package parts a module file lives in (`__init__.py` is its own package)."""
    return rel_path.removesuffix(".py").split("/")[:-1]


def _import_candidates(node: ast.Import | ast.ImportFrom, rel_path: str) -> list[str]:
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    if node.level == 0:
        base = node.module or ""
    else:
        package = _package_of(rel_path)
        if node.level - 1 > len(package):
            return []
        anchor = package[: len(package) - (node.level - 1)]
        base = ".".join([*anchor, *([node.module] if node.module else [])])
    return [base, *(f"{base}.{alias.name}" for alias in node.names if alias.name != "*")]


def _private_target(dotted: str, roots: tuple[str, ...], repo_root: Path) -> tuple[str, str] | None:
    """(`target`, `owner package`) for the first private component, if any.

    The owner is the package the private name belongs to: the prefix itself when
    it is a package directory (a private submodule), else that module's package
    (a module-level private name is package-private).
    """
    parts = dotted.split(".")
    if parts[0] not in roots:
        return None
    for index, part in enumerate(parts):
        if _is_private(part):
            prefix = parts[:index]
            owner = prefix if (repo_root / "/".join(prefix)).is_dir() else prefix[:-1]
            return ".".join(parts[: index + 1]), ".".join(owner)
    return None


def private_imports(
    tree: ast.Module, rel_path: str, roots: tuple[str, ...], repo_root: Path
) -> Sites:
    """Imports of a `_`-prefixed module or name from outside the package that owns it."""
    importer = ".".join(_package_of(rel_path))
    sites: Sites = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Import | ast.ImportFrom):
            continue
        targets = {
            found
            for dotted in _import_candidates(node, rel_path)
            if (found := _private_target(dotted, roots, repo_root)) is not None
        }
        for target, owner in sorted(targets):
            if importer != owner and not importer.startswith(f"{owner}."):
                sites.setdefault(f"{rel_path}::{target}", []).append(node.lineno)
    return sites


def _psycopg_scan(tree: ast.Module) -> tuple[set[str], set[str], list[ast.Call]]:
    """One walk: names bound to psycopg / its Connection classes, plus every call."""
    modules: set[str] = set()
    classes: set[str] = set()
    calls: list[ast.Call] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            calls.append(node)
        elif isinstance(node, ast.Import):
            modules.update(a.asname or a.name for a in node.names if a.name == "psycopg")
        elif isinstance(node, ast.ImportFrom) and node.module == "psycopg":
            classes.update(
                a.asname or a.name
                for a in node.names
                if a.name in {"Connection", "AsyncConnection"}
            )
    return modules, classes, calls


def _unsubscript(node: ast.expr) -> ast.expr:
    return node.value if isinstance(node, ast.Subscript) else node


def _postgres_dials(tree: ast.Module) -> list[int]:
    """Lines that open a Postgres transport: psycopg connects and pool constructions."""
    modules, classes, calls = _psycopg_scan(tree)
    lines: list[int] = []
    for node in calls:
        func = _unsubscript(node.func)
        if isinstance(func, ast.Attribute) and func.attr == "connect":
            target = _unsubscript(func.value)
            dial = (isinstance(target, ast.Name) and target.id in modules | classes) or (
                isinstance(target, ast.Attribute)
                and target.attr in {"Connection", "AsyncConnection"}
                and isinstance(target.value, ast.Name)
                and target.value.id in modules
            )
        else:
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
            dial = name.endswith("ConnectionPool")
        if dial:
            lines.append(node.lineno)
    return lines


@dataclass(frozen=True)
class Decision:
    """A design decision with exactly one owning module; other sites are bypasses."""

    owners: frozenset[str]
    find: Callable[[ast.Module], list[int]]
    fix: str
    # path -> one-line reason the site genuinely cannot go through the owner.
    allowed: dict[str, str] = field(default_factory=dict[str, str])


DECISIONS: dict[str, Decision] = {
    "postgres-dial": Decision(
        owners=frozenset({"shared/db_connections.py"}),
        find=_postgres_dials,
        fix=(
            "dial through shared.db.connect() / shared.db.pool(), which own the transport "
            "posture (prepare_threshold=None, keepalives, statement ceiling, sslmode, "
            "pooled-session scrub)"
        ),
    ),
}


def owner_bypasses(tree: ast.Module, rel_path: str) -> Sites:
    sites: Sites = {}
    for name, decision in DECISIONS.items():
        if rel_path in decision.owners or rel_path in decision.allowed:
            continue
        lines = decision.find(tree)
        if lines:
            sites[f"{rel_path}::{name}"] = lines
    return sites


def is_test_file(rel_path: str) -> bool:
    return any(pattern.search(rel_path) for pattern in _TEST_PATTERNS)


def measure(
    tree: ast.Module, rel_path: str, roots: tuple[str, ...], repo_root: Path
) -> dict[str, Sites]:
    if is_test_file(rel_path):
        return {kind: {} for kind in SECTIONS}
    return {
        "private_imports": private_imports(tree, rel_path, roots, repo_root),
        "owner_bypasses": owner_bypasses(tree, rel_path),
    }


def allowlist_errors(tree: ast.Module, rel_path: str) -> list[tuple[int, str]]:
    """A listed exemption whose module no longer bypasses the owner is stale."""
    return [
        (
            1,
            f"stale {name} allowlist entry — the module no longer bypasses the owner; "
            "remove it from DECISIONS in scripts/structure/locality.py",
        )
        for name, decision in DECISIONS.items()
        if rel_path in decision.allowed and not decision.find(tree)
    ]


def _new_site_message(kind: str, target: str) -> str:
    if kind == "private_imports":
        return (
            f"imports private `{target}` from outside the package that owns it — import a "
            "public name through the owner's package door (its __init__.py), or promote the "
            "name into the owner's contract deliberately (export it / drop the underscore)"
        )
    return f"bypasses the single owner of `{target}` — {DECISIONS[target].fix}"


def site_errors(
    measured: dict[str, Sites],
    baseline: dict[str, dict[str, int]],
    *,
    scanned: set[str],
    repo_root: Path,
    renames: dict[str, str] | None = None,
) -> list[str]:
    """Frozen counts must match reality: growth is a violation, shrinkage a stale entry."""
    sources = {new: old for old, new in (renames or {}).items()}
    errors: list[str] = []
    for kind in SECTIONS:
        errors.extend(_growth_errors(kind, measured[kind], baseline[kind], sources))
        errors.extend(_stale_errors(kind, measured[kind], baseline[kind], scanned, repo_root))
    return errors


def _growth_errors(
    kind: str, sites: Sites, frozen: dict[str, int], sources: dict[str, str]
) -> list[str]:
    errors: list[str] = []
    for key, lines in sorted(sites.items()):
        if len(lines) <= frozen.get(key, 0):
            continue
        path, _, target = key.partition("::")
        detail = _new_site_message(kind, target)
        if key in frozen:
            detail += f" (grew above its frozen count {frozen[key]})"
        elif path in sources:
            detail += f" (renamed file: migrate the baseline key from {sources[path]})"
        errors.extend(f"{path}:{line}: {detail}" for line in lines)
    return errors


def _stale_errors(
    kind: str, sites: Sites, frozen: dict[str, int], scanned: set[str], repo_root: Path
) -> list[str]:
    """Entries of scanned (or deleted) files whose sites shrank below the frozen count."""
    errors: list[str] = []
    for key, count in sorted(frozen.items()):
        path = key.partition("::")[0]
        if path not in scanned and (repo_root / path).exists():
            continue
        now = len(sites.get(key, []))
        if now < count:
            action = f"lower it to {now}" if now else "remove it"
            errors.append(
                f"{_BASELINE_PATH}: stale {kind} entry {key} is frozen at {count} but the "
                f"code has {now} — {action} (the baseline must match reality)"
            )
    return errors


def validate_entries(kind: str, entries: object, scope: tuple[str, ...]) -> None:
    if not isinstance(entries, dict):
        raise ValueError(f"'{kind}' must be an object")  # noqa: TRY004 — invalid JSON schema
    for key, count in cast("dict[str, object]", entries).items():
        path_text, separator, target = key.partition("::")
        path = Path(path_text)
        valid_path = (
            path.parts
            and not path.is_absolute()
            and path.as_posix() == path_text
            and ".." not in path.parts
            and path.parts[0] in scope
            and path.suffix == ".py"
        )
        valid_target = target in DECISIONS if kind == "owner_bypasses" else bool(target)
        if (
            not valid_path
            or not separator
            or not valid_target
            or type(count) is not int
            or count < 1
        ):
            raise ValueError(
                f"invalid {kind} entry {key!r}: expected a scoped .py path::target and integer >= 1"
            )
