"""Locality rules for the structure gate: package doors and single decision owners.

Both rules measure `path::target -> [line, ...]` sites per module; the frozen
counts live in the `private_imports` / `owner_bypasses` sections of
scripts/structure/baseline.json, and the rules themselves are documented in the
scripts/lint_code_structure.py header (Rules 4 and 5).
"""

from __future__ import annotations

import ast
import functools
import re
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

SECTIONS = ("private_imports", "owner_bypasses")
_BASELINE_PATH = "scripts/structure/baseline.json"
# White-box tests reach into privates by design; only test *directories* are
# exempt, since a governed module may legitimately be named test_*.py.
_TEST_DIR = re.compile(r"(^|/)tests?/")
Sites = dict[str, list[int]]


def _is_private(part: str) -> bool:
    return part.startswith("_") and not part.startswith("__")


def _package_of(rel_path: str) -> list[str]:
    """Dotted package parts a module file lives in (`__init__.py` is its own package)."""
    return rel_path.removesuffix(".py").split("/")[:-1]


@functools.cache
def _entries(directory: Path, mtime_ns: int) -> frozenset[str]:
    """Names in a directory; keyed on its mtime, so adding or removing an entry
    invalidates the cached listing."""
    del mtime_ns  # cache key only
    return frozenset(entry.name for entry in directory.iterdir())


def reset_caches() -> None:
    """Forget cached directory listings. Each gate run starts here: two writes in
    one coarse mtime tick would otherwise leave a long-lived caller a stale listing."""
    _entries.cache_clear()


def _exists_exact(path: Path, *, directory: bool) -> bool:
    """Case-exact existence, so a case-folding filesystem (macOS) agrees with CI."""
    found = path.is_dir() if directory else path.is_file()
    return found and path.name in _entries(path.parent, path.parent.stat().st_mtime_ns)


def _is_module(dotted: str, repo_root: Path) -> bool:
    path = repo_root / dotted.replace(".", "/")
    return _exists_exact(path.with_name(f"{path.name}.py"), directory=False) or _exists_exact(
        path, directory=True
    )


def _import_base(node: ast.ImportFrom, rel_path: str) -> str | None:
    if node.level == 0:
        return node.module or ""
    package = _package_of(rel_path)
    if node.level - 1 > len(package):
        return None
    anchor = package[: len(package) - (node.level - 1)]
    return ".".join([*anchor, *([node.module] if node.module else [])])


def _import_candidates(node: ast.Import | ast.ImportFrom, rel_path: str) -> list[str]:
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    base = _import_base(node, rel_path)
    if base is None:
        return []
    return [base, *(f"{base}.{alias.name}" for alias in node.names if alias.name != "*")]


def _module_aliases(
    node: ast.Import | ast.ImportFrom, rel_path: str, repo_root: Path
) -> dict[str, str]:
    """Local names this import binds to a module or package (not to a function or class)."""
    if isinstance(node, ast.Import):
        return {
            alias.asname or alias.name.split(".")[0]: alias.name
            if alias.asname
            else alias.name.split(".")[0]
            for alias in node.names
        }
    base = _import_base(node, rel_path)
    if not base:
        return {}
    return {
        alias.asname or alias.name: f"{base}.{alias.name}"
        for alias in node.names
        if alias.name != "*" and _is_module(f"{base}.{alias.name}", repo_root)
    }


def _attribute_target(node: ast.Attribute, aliases: dict[str, str], repo_root: Path) -> str | None:
    """`module._name` reached by attribute access on a module alias (`ava.agent_identity.x`)."""
    attrs: list[str] = []
    current: ast.expr = node
    while isinstance(current, ast.Attribute):
        attrs.append(current.attr)
        current = current.value
    if not isinstance(current, ast.Name) or current.id not in aliases:
        return None
    dotted = aliases[current.id]
    for attr in reversed(attrs):
        if not _is_module(dotted, repo_root):
            return None  # past the module boundary: an attribute of a function or class
        dotted = f"{dotted}.{attr}"
        if _is_private(attr):
            return dotted
    return None


def _private_target(dotted: str, roots: tuple[str, ...], repo_root: Path) -> tuple[str, str] | None:
    """(`target`, `owner package`) for the first private component, if any.

    Resolved the way Python does: when `<prefix>.py` exists the prefix is a module,
    so the private name is package-private to that module's package; otherwise the
    prefix is the package that owns the private submodule. A same-named directory
    beside a module (an OKF docs folder, a stale __pycache__) never changes this.
    """
    parts = dotted.split(".")
    if parts[0] not in roots:
        return None
    for index, part in enumerate(parts):
        if _is_private(part):
            prefix = parts[:index]
            module_file = repo_root / f"{'/'.join(prefix)}.py"
            owner = prefix[:-1] if _exists_exact(module_file, directory=False) else prefix
            return ".".join(parts[: index + 1]), ".".join(owner)
    return None


@dataclass
class _Reach:
    rel_path: str
    importer: str
    roots: tuple[str, ...]
    repo_root: Path
    sites: Sites = field(default_factory=dict[str, list[int]])

    def record(self, lineno: int, candidates: list[str]) -> None:
        targets = {
            found
            for dotted in candidates
            if (found := _private_target(dotted, self.roots, self.repo_root)) is not None
        }
        for target, owner in sorted(targets):
            inside = self.importer == owner or self.importer.startswith(f"{owner}.")
            if not inside:
                self.sites.setdefault(f"{self.rel_path}::{target}", []).append(lineno)


def private_imports(
    tree: ast.Module, rel_path: str, roots: tuple[str, ...], repo_root: Path
) -> Sites:
    """Imports of, or attribute reach-ins to, a `_`-prefixed module or name from
    outside the package that owns it."""
    reach = _Reach(rel_path, ".".join(_package_of(rel_path)), roots, repo_root)
    aliases: dict[str, str] = {}
    rebound: set[str] = set()
    attributes: list[ast.Attribute] = []
    inner: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            attributes.append(node)
            inner.add(id(node.value))
        elif isinstance(node, ast.Import | ast.ImportFrom):
            aliases.update(_module_aliases(node, rel_path, repo_root))
            reach.record(node.lineno, _import_candidates(node, rel_path))
        elif isinstance(node, ast.arg) or (
            isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store)
        ):
            rebound.add(node.arg if isinstance(node, ast.arg) else node.id)
    # Scope-free: a name rebound anywhere in the module (a parameter such as
    # `self`, a local, a loop target) may shadow the import, so it is skipped.
    aliases = {name: dotted for name, dotted in aliases.items() if name not in rebound}
    for node in attributes:
        if id(node) not in inner:  # outermost link of each chain only
            target = _attribute_target(node, aliases, repo_root)
            if target is not None:
                reach.record(node.lineno, [target])
    return reach.sites


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


def owner_bypasses(tree: ast.Module, rel_path: str, roots: tuple[str, ...]) -> Sites:
    sites: Sites = {}
    for name, decision in DECISIONS.items():
        if rel_path in decision.owners or rel_path in decision.allowed:
            continue
        lines = decision.find(tree, roots)
        if lines:
            sites[f"{rel_path}::{name}"] = lines
    return sites


def measure(
    tree: ast.Module, rel_path: str, roots: tuple[str, ...], repo_root: Path
) -> dict[str, Sites]:
    if _TEST_DIR.search(rel_path):
        return {kind: {} for kind in SECTIONS}
    return {
        "private_imports": private_imports(tree, rel_path, roots, repo_root),
        "owner_bypasses": owner_bypasses(tree, rel_path, roots),
    }


def allowlist_errors(
    tree: ast.Module, rel_path: str, roots: tuple[str, ...]
) -> list[tuple[int, str]]:
    """A listed exemption whose module no longer bypasses the owner is stale."""
    return [
        (1, f"stale {name} allowlist entry — the module no longer bypasses the owner; {_UNLIST}")
        for name, decision in DECISIONS.items()
        if rel_path in decision.allowed and not decision.find(tree, roots)
    ]


_UNLIST = "remove it from DECISIONS in scripts/structure/locality.py"


def missing_allowlist_errors(repo_root: Path) -> list[str]:
    """A listed exemption for a module that no longer exists is stale too."""
    return [
        f"{path}:1: stale {name} allowlist entry — the module no longer exists; {_UNLIST}"
        for name, decision in DECISIONS.items()
        for path in sorted(decision.allowed)
        if not (repo_root / path).is_file()
    ]


def _new_site_message(kind: str, target: str) -> str:
    if kind == "private_imports":
        return (
            f"reaches private `{target}` from outside the package that owns it — use a "
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
        errors.extend(f"{path}:{line}: {detail}" for line in sorted(lines))
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


def unpaired_additions(current: dict[str, int], previous: dict[str, int]) -> list[str]:
    """Added keys not explained by a same-file removal of the same private name.

    The one legitimate new key is a moved owner: `f::a._x` becoming `f::a.b._x`
    with an equal or lower count. Pairing on the private leaf name (not just the
    file) keeps a PR from trading a frozen reach-in for an unrelated new one.
    """
    removals: dict[tuple[str, str], list[int]] = defaultdict(list)
    for key in previous.keys() - current.keys():
        removals[_pair_key(key)].append(previous[key])
    for candidates in removals.values():
        candidates.sort()
    unmatched: list[str] = []
    for key in sorted(current.keys() - previous.keys(), key=lambda k: (current[k], k)):
        candidates = removals[_pair_key(key)]
        match = next((i for i, value in enumerate(candidates) if value >= current[key]), None)
        if match is None:
            unmatched.append(key)
        else:
            candidates.pop(match)
    return unmatched


def _pair_key(key: str) -> tuple[str, str]:
    path, _, target = key.partition("::")
    return path, target.rsplit(".", 1)[-1]


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
