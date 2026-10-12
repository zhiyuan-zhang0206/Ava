"""Locality rules for the structure gate: package doors and single decision owners.

Both rules measure `path::target -> [line, ...]` sites per module and reject every
measured site without baseline allowances. The rules themselves are documented
in scripts/lint/code_structure.py (Rules 4 and 5).
"""

from __future__ import annotations

import ast
import functools
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

from meizel.language import path_imports
from meizel.owners import decisions
from meizel.state import ambient_state
from scripts.structure import baseline_shards, imports
from scripts.structure.imports import bindings

SECTIONS = ("private_imports", "owner_bypasses")
STRICT_SECTIONS = ("private_imports", "owner_bypasses", path_imports.SECTION)
# No externally measured rule retains baseline allowances.
EXTERNAL_SECTIONS: dict[str, str] = {}
# White-box tests reach into privates by design; only test *directories* are
# exempt, since a governed module may legitimately be named test_*.py.
_TEST_DIR = re.compile(r"(^|/)tests?/")
Sites = dict[str, list[int]]


def _is_private(part: str) -> bool:
    return part.startswith("_") and not part.startswith("__")


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


def _attribute_target(node: ast.Attribute, aliases: dict[str, str], repo_root: Path) -> str | None:
    """`module._name` reached by attribute access on a module alias (`module._member`)."""
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
    reach = _Reach(rel_path, ".".join(imports.package_of(rel_path)), roots, repo_root)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import | ast.ImportFrom):
            reach.record(node.lineno, imports.normalize(node, rel_path).candidates)
    visitor = _PrivateReach(tree, reach)
    visitor.visit(tree)
    return reach.sites


class _PrivateReach(ast.NodeVisitor):
    def __init__(self, tree: ast.Module, reach: _Reach) -> None:
        self.reach = reach
        self.scope = bindings.Scope(tree, reach.rel_path)

    def _nested(self, node: bindings.ScopeNode) -> None:
        parent = self.scope
        outer, inner = bindings.scope_parts(node)
        for expression in outer:
            self.visit(expression)
        self.scope = bindings.Scope(node, self.reach.rel_path, parent.nested_parent())
        for statement in inner:
            self.visit(statement)
        self.scope = parent

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._nested(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._nested(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._nested(node)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        self._nested(node)

    def visit_ListComp(self, node: ast.ListComp) -> None:
        self._nested(node)

    def visit_SetComp(self, node: ast.SetComp) -> None:
        self._nested(node)

    def visit_DictComp(self, node: ast.DictComp) -> None:
        self._nested(node)

    def visit_GeneratorExp(self, node: ast.GeneratorExp) -> None:
        self._nested(node)

    def visit_Import(self, node: ast.Import) -> None:
        pass

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        pass

    def visit_Attribute(self, node: ast.Attribute) -> None:
        root: ast.expr = node
        while isinstance(root, ast.Attribute):
            root = root.value
        if isinstance(root, ast.Name):
            origin = self.scope.origin(root)
            if origin and _is_module(origin, self.reach.repo_root):
                target = _attribute_target(node, {root.id: origin}, self.reach.repo_root)
                if target is not None:
                    self.reach.record(node.lineno, [target])
        # One outermost attribute chain is one reach-in site.
        if not isinstance(node.value, ast.Attribute):
            self.visit(node.value)


def measure(
    tree: ast.Module, rel_path: str, roots: tuple[str, ...], repo_root: Path
) -> dict[str, Sites]:
    if _TEST_DIR.search(rel_path):
        return {kind: {} for kind in SECTIONS if kind not in EXTERNAL_SECTIONS}
    return {
        "private_imports": private_imports(tree, rel_path, roots, repo_root),
        "owner_bypasses": decisions.owner_bypasses(tree, rel_path, roots),
    }


def _new_site_message(kind: str, target: str) -> str:
    if kind == "private_imports":
        return (
            f"reaches private `{target}` from outside the package that owns it — use a "
            "public name through the owner's package door (its __init__.py), or promote the "
            "name into the owner's contract deliberately (export it / drop the underscore)"
        )
    if kind == path_imports.SECTION:
        return f"imports by file path (`{target}`) — {path_imports.FIX}"
    if kind == ambient_state.SECTION:
        return ambient_state.site_message(target)
    return f"bypasses the single owner of `{target}` — {decisions.DECISIONS[target].fix}"


def site_errors(
    measured: dict[str, Sites],
    baseline: dict[str, dict[str, int]],
    *,
    scanned: set[str],
    repo_root: Path,
    renames: dict[str, str] | None = None,
) -> list[str]:
    """Reject every locality/path-import site; guard remaining ambient-state debt."""
    sources = {new: old for old, new in (renames or {}).items()}
    errors: list[str] = []
    for kind, sites in measured.items():
        if kind in EXTERNAL_SECTIONS:
            continue
        if kind in STRICT_SECTIONS:
            errors.extend(_growth_errors(kind, sites, {}, {}))
            continue
        errors.extend(_growth_errors(kind, sites, baseline[kind], sources))
        errors.extend(_stale_errors(kind, sites, baseline[kind], scanned, repo_root))
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
                f"{baseline_shards.shard_path(kind, key)}: stale {kind} entry {key} is frozen at {count} but the "
                f"code has {now} — {action} (the baseline must match reality)"
            )
    return errors


def _entry_scope(kind: str, scope: tuple[str, ...]) -> tuple[str, ...]:
    """Top-level directories an entry's path may start with: an externally measured section
    freezes test files, which sit in `tests/` or inside a package."""
    return (*scope, "tests", "scripts") if kind in EXTERNAL_SECTIONS else scope


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


def _valid_target(kind: str, target: str) -> bool:
    if kind == "owner_bypasses":
        return target in decisions.DECISIONS
    return ambient_state.is_target(target) if kind == ambient_state.SECTION else bool(target)


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
            and path.parts[0] in _entry_scope(kind, scope)
            and path.suffix == ".py"
        )
        valid_target = _valid_target(kind, target)
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
