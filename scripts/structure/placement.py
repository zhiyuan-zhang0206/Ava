"""Where a test file belongs: the package its own first-party references allow.

The placement rule (one owner, reused by every test-locality lint):

1. Collect the first-party modules the file *tests*: import statements (also inside
   functions), string targets of `importlib.import_module` / `importorskip` and friends,
   paths below the repository root naming a source file (`ROOT / "scripts" / "lint" /
   "x.py"`) and imports in source strings the file runs. Evidence that exists only because
   the file *patches* something, or only because it carries sample data, does not count
   (see "Patch evidence" and "Data is not evidence").
2. Each module belongs to a *unit*: a top-level package, except `services.<x>`, which is a
   unit of its own (import-linter says `services` is not a layer).
3. The home unit is the referenced unit that may legally import every other referenced
   unit: the import-linter layers and forbidden contracts in `pyproject.toml` decide, and a
   pair the contracts are silent about falls back to the direction the non-test source
   already imports (computed lazily, only when a pair needs it). A set of units with no
   legal top is ambiguous; the pick prefers the unit of the module the file is named after
   (`test_<module>.py`), then the unit that may import the most others.
4. Inside the home unit the home package is the deepest package P that holds or directly
   depends on every referenced module of that unit: each such module lies in P's subtree,
   or the non-test code in P's subtree imports it (or something below it) directly, function-level
   imports included. Candidates are the packages on the ancestor chains of the referenced
   modules, never above their nearest common ancestor directory (the bound). Only direct
   imports count (a dependency of a dependency does not), and `tests.*`/`<pkg>.tests.*` references
   are test support, not modules of the unit: a shared helper never raises or lowers the home. If no single
   package is the deepest (a dependency cycle between two packages), the home stays at the
   bound. Layer legality stays with step 3: only the home unit's own packages are
   candidates, so a production import running against the contracts moves no home. A file
   whose evidence spans packages that no one package depends on therefore lives high: it
   is an integration test of those packages, not a unit test of one.

Files that stay at the top level by policy (`TOP_LEVEL_*`: e2e, shared fixtures and
factories, the root conftest, the real-process integration proofs) have no home: there is
no package below the top level they belong to. A file with no first-party reference has
none either.

## Patch evidence

The home is where the file's *subject* lives. A module that is imported only to be
replaced is not its subject, so two kinds of evidence are dropped:

- the string target of a patch (`monkeypatch.setattr("a.b.c", ...)`, `patch("a.b.c")`,
  `patch.dict`, `patch.multiple`);
- an import whose bound name is only ever read as the first argument of a patch call
  (`monkeypatch.setattr(mod, "x", v)`, `patch.object(mod, "x")`, `setitem(mod.d, ...)`).

Without this, a test in package X that patches a private name of a descendant package Y
would have the placement rule move its home up to a package that owns Y, and the reach-in
would read as "own package". Fallback (documented, tested): when every strong first-party
reference of a file is patch evidence, nothing is dropped, so such a file keeps the home
its patch targets give it rather than losing its home.

## Data is not evidence

A test of a linter or a gate carries sample paths and sample source: the input of its subject,
not the subject. A path counts only as a chain from the repository root (`repo_root()`, a name
bound only to it, a climb from `Path(__file__)` ending exactly there): `tmp_path / "scripts"`
and a bare `"scripts/x.py"` name no root. Source in a string counts only in a file that spawns
an interpreter (`sys.executable`). Both gates: `scripts/structure/placement_evidence.py`.

## What a home depends on

A result depends on the file's own text, `pyproject.toml` and the non-test source's direct
imports (`ModuleIndex.importers`, read from the working tree on every run and cached per file
by `import_cache.py`; no dependency graph is committed). Moving the file into `<pkg>/tests/`
does not change its home, but a production import change can: adding an import may lower the
home of a test that references both ends, deleting one may raise it, and a
dependency cycle keeps it at the bound. Because of that the lint that enforces a home (the
patch-target lint) checks a changed test file alone at commit time and rescans everything at
push and in CI; and the rule that will enforce a test's placement must require the *legal*
directory, never the *lowest* one, or an unrelated production commit would force tests to move.
"""

from __future__ import annotations

import ast
import collections
import functools
import itertools
import re
import tomllib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

from scripts.structure import import_cache, lint_common, placement_evidence, service_units

# First-party code tops that take part in placement (import-linter roots + scripts).
CODE_TOPS = (*lint_common.FRAMEWORK_DIRS, "scripts")
# Further first-party tops a test may patch into; they take no part in placement.
PATCH_TOPS = (*CODE_TOPS, "schedules", "commands", "demos")
_ARTIFACT_TOPS = (
    "schedules",
    "commands",
    "demos",
    "db",
    "deploy",
    "ui",
    "migrations",
    ".github",
    "docs",
    "okf",
    "future",
    ".agents",
    "assets",
)
_ROOT_FILES = (
    "pyproject.toml",
    "uv.lock",
    ".pre-commit-config.yaml",
    "AGENTS.md",
    ".gitleaks.toml",
    "CHANGELOG.md",
    ".test_durations",
)
_PATH_ROOTS = (*CODE_TOPS, *_ARTIFACT_TOPS, *_ROOT_FILES)

# Tests that belong at the top level: no package below it is their home.
TOP_LEVEL_PREFIXES = ("tests/e2e/", "tests/fixtures/", "tests/factories/")
TOP_LEVEL_FILES = frozenset(
    {
        "conftest.py",
        "tests/conftest.py",
        # Real-process integration proofs with their own CI wiring.
        "tests/integration/test_grafana_native_runtime.py",
        "tests/integration/test_cluster_instance.py",
        "tests/integration/test_schedule_runner_cleanup.py",
    }
)

STRONG_KINDS = frozenset({"import", "string-target", "embedded-import", "path-file"})
_STRING_TARGET_CALLEES = frozenset(
    {
        "setattr",
        "delattr",
        "patch",
        "dict",
        "multiple",
        "import_module",
        "importorskip",
        "__import__",
        "find_spec",
        "run_module",
        "resolve_name",
        "reload",
    }
)
# The subset of string-target callees whose target is *replaced*, not imported.
_PATCH_CALLEES = frozenset({"setattr", "delattr", "patch", "dict", "multiple"})
# `<receiver>.setattr(...)` is a monkeypatch call unless the receiver is one of these.
_NOT_MONKEYPATCH = frozenset({"self", "cls", "os", "sys", "object", "builtins", "super"})
_MONKEYPATCH_TARGET_CALLS = frozenset({"setattr", "delattr", "setitem", "delitem"})
_DOTTED = re.compile(r"^[A-Za-z_]\w*(\.[A-Za-z_]\w*)+$")
_EMBEDDED_IMPORT = re.compile(
    r"^[ \t]*(?:from[ \t]+([A-Za-z_][\w.]*)[ \t]+import[ \t]+([^\n#]+)"
    r"|import[ \t]+([A-Za-z_][\w.]*(?:[ \t]*,[ \t]*[A-Za-z_][\w.]*)*))",
    re.MULTILINE,
)


def unit_of(module: str) -> str | None:
    parts = module.split(".")
    if parts[0] not in CODE_TOPS:
        return None
    if parts[0] == "services" and len(parts) > 1:
        return service_units.unit_of(parts)
    return parts[0]


def unit_root(unit: str) -> str:
    return unit.replace(".", "/")


# --------------------------------------------------------------------------- module index


class ModuleIndex:
    """Resolves dotted names and path literals against a checkout."""

    def __init__(self, repo_root: Path) -> None:
        self.repo_root = repo_root
        self._kinds: dict[str, str | None] = {}
        self._prefixes: dict[str, str | None] = {}

    def kind(self, dotted: str) -> str | None:
        """'file' | 'pkg' | 'ns' | None for a dotted module path."""
        if dotted in self._kinds:
            return self._kinds[dotted]
        path = self.repo_root.joinpath(*dotted.split("."))
        found: str | None = None
        if path.with_suffix(".py").is_file():
            found = "file"
        elif (path / "__init__.py").is_file():
            found = "pkg"
        elif path.is_dir() and any(path.rglob("*.py")):
            found = "ns"
        self._kinds[dotted] = found
        return found

    def resolve_prefix(self, dotted: str, tops: Sequence[str] = CODE_TOPS) -> str | None:
        """Longest dotted prefix that is a module; None if the top segment is not code."""
        parts = dotted.split(".")
        if parts[0] not in tops:
            return None
        if dotted not in self._prefixes:
            self._prefixes[dotted] = next(
                (
                    candidate
                    for length in range(len(parts), 0, -1)
                    if self.kind(candidate := ".".join(parts[:length]))
                ),
                None,
            )
        return self._prefixes[dotted]

    def split(self, dotted: str) -> tuple[str, list[str]] | None:
        """(longest first-party module prefix, remaining attribute segments), or None."""
        module = self.resolve_prefix(dotted, PATCH_TOPS)
        if module is None:
            return None
        return module, dotted[len(module) :].split(".")[1:]

    @functools.cached_property
    def importers(self) -> dict[str, set[str]]:
        """Module prefix -> the directories whose non-test subtree directly imports it.

        `a.b.c` imported from `x/y/z.py` files `a`, `a.b` and `a.b.c` under `x`, `x/y`. Direct
        edges only, function-level imports included; resolved against this checkout.
        """
        found: dict[str, set[str]] = collections.defaultdict(set)
        for rel, statements in import_cache.production_imports(self.repo_root, CODE_TOPS).items():
            parts = rel.split("/")[:-1]
            directories = {"/".join(parts[:depth]) for depth in range(1, len(parts) + 1)}
            for ref in collect_references(ast.parse(statements), self):
                if ref.kind == "import":
                    names = ref.module.split(".")
                    for depth in range(1, len(names) + 1):
                        found[".".join(names[:depth])] |= directories
        return found

    def dir_of(self, module: str) -> str:
        """Directory owning the module: its parent dir for a file, itself for a package."""
        parts = module.split(".")
        return "/".join(parts[:-1]) if self.kind(module) == "file" else "/".join(parts)

    def path_to_module(self, rel: str) -> str | None:
        """Repo-relative path literal -> nearest enclosing dotted module (code dirs only)."""
        parts = rel.strip("/").split("/")
        if parts[-1].endswith(".py"):
            parts[-1] = parts[-1][:-3]
        if parts[0] not in CODE_TOPS:
            return None
        while parts:
            candidate = ".".join(parts)
            if all(re.fullmatch(r"[A-Za-z_]\w*", part) for part in parts) and self.kind(candidate):
                return candidate
            parts.pop()
        return None


# --------------------------------------------------------------------------- unit graph


@dataclass
class UnitGraph:
    """Which unit may import which: contracts first, then per-pair source evidence."""

    repo_root: Path
    units: list[str]
    forbidden: set[tuple[str, str]] = field(default_factory=set[tuple[str, str]])
    reach: dict[str, set[str]] = field(default_factory=dict[str, set[str]])

    @functools.cached_property
    def empirical(self) -> collections.Counter[tuple[str, str]]:
        """Import edges between units in the non-test source (computed on first need)."""
        edges: collections.Counter[tuple[str, str]] = collections.Counter()
        for top in CODE_TOPS:
            for path in (self.repo_root / top).rglob("*.py"):
                rel = path.relative_to(self.repo_root)
                if "tests" in rel.parts or "docs" in rel.parts:
                    continue
                source = unit_of(".".join(rel.with_suffix("").parts)) or top
                try:
                    tree = ast.parse(path.read_text(encoding="utf-8"))
                except (SyntaxError, UnicodeDecodeError):
                    continue
                for module in _imported_modules(tree):
                    target = unit_of(module)
                    if target and target != source:
                        edges[source, target] += 1
        return edges

    def can_import(self, a: str, b: str) -> bool:
        """May code in unit `a` import unit `b`?"""
        if a == b:
            return True
        if (a, b) in self.forbidden:
            return False
        if b in self.reach[a]:
            return True
        if a in self.reach[b]:
            return False
        forward, backward = self.empirical[a, b], self.empirical[b, a]
        return forward > 0 and forward > backward

    def blocked(self, a: str, b: str) -> bool:
        """Config says code in `a` must not import `b`."""
        return (a, b) in self.forbidden or a in self.reach[b]


def _imported_modules(tree: ast.AST) -> Iterable[str]:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            yield from (alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            yield node.module


def _matching_units(name: str, units: list[str]) -> list[str]:
    return [unit for unit in units if unit == name or unit.startswith(name + ".")]


def _build_units(repo_root: Path) -> list[str]:
    units = {top for top in CODE_TOPS if top != "services"}
    units.add("services")
    units |= service_units.build(repo_root / "services")
    return sorted(units)


def _unit_pairs(graph: UnitGraph, higher: list[str], lower: list[str]) -> set[tuple[str, str]]:
    """Every (unit matching a `higher` name, unit matching a `lower` name) pair."""
    return {
        (hu, lu)
        for hi in higher
        for lo in lower
        for hu in _matching_units(hi, graph.units)
        for lu in _matching_units(lo, graph.units)
    }


def _layers_contract(graph: UnitGraph, contract: dict[str, object]) -> set[tuple[str, str]]:
    """Register one layers contract; returns its (higher, lower) edges."""
    layers = [[part.strip() for part in entry.split("|")] for entry in _strings(contract["layers"])]
    for group in layers:  # `a | b` siblings are independent: neither may import the other
        for a, b in itertools.permutations(group, 2):
            graph.forbidden |= _unit_pairs(graph, [a], [b])
    edges: set[tuple[str, str]] = set()
    for index, higher in enumerate(layers):
        for lower in layers[index + 1 :]:
            edges |= _unit_pairs(graph, higher, lower)
    return edges


def _forbidden_contract(graph: UnitGraph, contract: dict[str, object]) -> None:
    exempt: set[str] = set()
    for ignore in _strings(contract.get("ignore_imports", [])):
        left = ignore.split("->")[0].strip()
        exempt.add(left.split(".*")[0].rstrip("*").rstrip("."))
    for source in _strings(contract["source_modules"]):
        for source_unit in _matching_units(source, graph.units):
            if source_unit in exempt or any(source_unit.startswith(e + ".") for e in exempt):
                continue
            for forbidden in _strings(contract["forbidden_modules"]):
                graph.forbidden.update(
                    (source_unit, target) for target in _matching_units(forbidden, graph.units)
                )


def _strings(value: object) -> list[str]:
    if not isinstance(value, list):
        raise TypeError(f"import-linter contract field must be a list, got {type(value).__name__}")
    return [str(item) for item in cast("list[object]", value)]


@functools.cache
def unit_graph(repo_root: Path) -> UnitGraph:
    """The unit graph derived from `[tool.importlinter]` in pyproject.toml (read once)."""
    config = tomllib.loads((repo_root / "pyproject.toml").read_text(encoding="utf-8"))
    contracts: list[dict[str, object]] = config["tool"]["importlinter"]["contracts"]
    graph = UnitGraph(repo_root, _build_units(repo_root))
    edges: set[tuple[str, str]] = set()
    for contract in contracts:
        if contract["type"] == "layers":
            edges |= _layers_contract(graph, contract)
        elif contract["type"] == "forbidden":
            _forbidden_contract(graph, contract)
    adjacency: dict[str, set[str]] = {unit: set() for unit in graph.units}
    for higher, lower in edges:
        adjacency[higher].add(lower)
    for unit in graph.units:  # transitive closure over the CONFIG edges only
        seen: set[str] = set()
        stack = list(adjacency[unit])
        while stack:
            current = stack.pop()
            if current not in seen:
                seen.add(current)
                stack.extend(adjacency[current])
        graph.reach[unit] = seen - {unit}
    return graph


# --------------------------------------------------------------------------- references


@dataclass
class Ref:
    """One first-party module a file references."""

    line: int
    kind: str  # import | string-target | string-loose | embedded-import | path-file | path-dir
    module: str
    unit: str
    via: str = ""  # callee of a string target
    names: tuple[str, ...] = ()  # local names an import binds (patch-evidence pruning)


def _callee(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _callee(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    return ""


def _const_strings(args: Sequence[ast.expr]) -> list[str] | None:
    """The literal string arguments, or None unless every argument is one."""
    if args and all(isinstance(a, ast.Constant) and isinstance(a.value, str) for a in args):
        return [str(a.value) for a in args if isinstance(a, ast.Constant)]
    return None


def _div_operands(node: ast.AST) -> list[str | None]:
    """Left-to-right operands of a `/` chain; literals as str, everything else None."""
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        return _div_operands(node.left) + _div_operands(node.right)
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    return [None]


class _Collector(ast.NodeVisitor):
    def __init__(self, index: ModuleIndex, tree: ast.AST, rel_path: str) -> None:
        self.index = index
        self.refs: list[Ref] = []
        self._handled: set[int] = set()
        self._div_seen: set[int] = set()
        self._roots = placement_evidence.RepoRoots(tree, rel_path)
        self._spawns_python = placement_evidence.spawns_interpreter(tree)

    def _add(
        self, line: int, kind: str, module: str, via: str = "", names: tuple[str, ...] = ()
    ) -> None:
        unit = unit_of(module) if "tests" not in module.split(".") else None
        if unit:
            self.refs.append(Ref(line, kind, module, unit, via, names))

    def _add_dotted(
        self, line: int, kind: str, dotted: str, via: str = "", names: tuple[str, ...] = ()
    ) -> None:
        module = self.index.resolve_prefix(dotted)
        if module:
            self._add(line, kind, module, via, names)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            top = alias.name.split(".")[0]
            if top in CODE_TOPS:
                self._add_dotted(node.lineno, "import", alias.name, names=(alias.asname or top,))

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.level or not node.module or node.module.split(".")[0] not in CODE_TOPS:
            return
        for alias in node.names:
            candidate = f"{node.module}.{alias.name}"
            names = (alias.asname or alias.name,)
            if self.index.kind(candidate):
                self._add(node.lineno, "import", candidate, names=names)
            else:
                self._add_dotted(node.lineno, "import", node.module, names=names)

    def visit_Call(self, node: ast.Call) -> None:
        callee = _callee(node.func)
        last = callee.rsplit(".", 1)[-1]
        constants = _const_strings(node.args)
        if last == "load_skill_script" and constants:
            self._note_path(node.lineno, "ava_builtins/skills/" + "/".join(constants))
        if (
            last == "joinpath"
            and constants
            and constants[0] in _PATH_ROOTS
            and isinstance(node.func, ast.Attribute)
            and self._roots.is_root(node.func.value)
        ):
            self._note_path(node.lineno, "/".join(c.strip("/") for c in constants))
        if last in _STRING_TARGET_CALLEES and node.args:
            first = node.args[0]
            if (
                isinstance(first, ast.Constant)
                and isinstance(first.value, str)
                and _DOTTED.match(first.value)
            ):
                self._add_dotted(node.lineno, "string-target", first.value, via=callee)
                self._handled.add(id(first))
        self.generic_visit(node)

    def visit_BinOp(self, node: ast.BinOp) -> None:
        if isinstance(node.op, ast.Div) and id(node) not in self._div_seen:
            inner: ast.expr = node
            while isinstance(inner, ast.BinOp):
                self._div_seen.add(id(inner))
                inner = inner.left
            chunk: list[str] = []
            for operand in _div_operands(node)[1:]:  # `inner` is the leftmost operand
                if operand is None:
                    break
                chunk.append(operand.strip("/"))
            if chunk and chunk[0].split("/")[0] in _PATH_ROOTS and self._roots.is_root(inner):
                self._note_path(node.lineno, "/".join(chunk))
        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> None:
        value = node.value
        if not isinstance(value, str):
            return
        if "\n" in value and "import " in value and self._spawns_python:
            self._embedded_imports(node.lineno, value)
        if len(value) > 200 or "\n" in value or " " in value or id(node) in self._handled:
            return
        if _DOTTED.match(value) and value.split(".")[0] in CODE_TOPS:
            module = self.index.resolve_prefix(value)
            if module and module.count(".") >= 1:
                self._add(node.lineno, "string-loose", module)
                return

    def _note_path(self, line: int, rel: str) -> None:
        if rel.split("/", maxsplit=1)[0] not in CODE_TOPS:
            return
        module = self.index.path_to_module(rel)
        if module:
            is_file = rel.endswith(".py") and (self.index.repo_root / rel).is_file()
            self._add(line, "path-file" if is_file else "path-dir", module)

    def _embedded_imports(self, line: int, text: str) -> None:
        for match in _EMBEDDED_IMPORT.finditer(text):
            if match.group(1):
                self._embedded_from(line, match.group(1), match.group(2))
            else:
                for name in match.group(3).split(","):
                    self._add_dotted(line, "embedded-import", name.strip())

    def _embedded_from(self, line: int, module: str, imported: str) -> None:
        if module.split(".", maxsplit=1)[0] not in CODE_TOPS:
            return
        names = [name.strip().split(" as ")[0].strip("() ") for name in imported.split(",")]
        submodules = [
            f"{module}.{name}" for name in names if name and self.index.kind(f"{module}.{name}")
        ]
        for submodule in submodules:
            self._add(line, "embedded-import", submodule)
        if not submodules:
            self._add_dotted(line, "embedded-import", module)


def collect_references(tree: ast.AST, index: ModuleIndex, rel_path: str = "") -> list[Ref]:
    """Every first-party reference of a parsed file, patch evidence included.

    `rel_path` (repo-relative, POSIX) says which climb from `Path(__file__)` is the repo root.
    """
    collector = _Collector(index, tree, rel_path)
    collector.visit(tree)
    return collector.refs


def _patch_target_root(call: ast.Call) -> ast.Name | None:
    """The Name at the root of a patch call's object argument (`mod`, `mod.sub.attr`)."""
    name = _callee(call.func)
    last = name.rsplit(".", 1)[-1]
    receiver = name.rsplit(".", 1)[0] if "." in name else ""
    monkeypatch = (
        bool(receiver)
        and receiver.split(".")[0] not in _NOT_MONKEYPATCH
        and last in _MONKEYPATCH_TARGET_CALLS
    )
    mock = name.endswith((".patch.object", ".patch.dict", ".patch.multiple")) or name in {
        "patch.object",
        "patch.dict",
        "patch.multiple",
    }
    if not (monkeypatch or mock) or not call.args:
        return None
    node = call.args[0]
    while isinstance(node, ast.Attribute):
        node = node.value
    return node if isinstance(node, ast.Name) else None


def _patch_only_names(nodes: Sequence[ast.AST]) -> set[str]:
    """Names read only as the object a patch call replaces something on."""
    roots = {
        id(root)
        for node in nodes
        if isinstance(node, ast.Call) and (root := _patch_target_root(node)) is not None
    }
    patched: collections.Counter[str] = collections.Counter()
    other: collections.Counter[str] = collections.Counter()
    for node in nodes:
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            (patched if id(node) in roots else other)[node.id] += 1
    return {name for name in patched if other[name] == 0}


def _has_strong(refs: Iterable[Ref]) -> bool:
    return any(ref.kind in STRONG_KINDS for ref in refs)


def without_patch_evidence(nodes: Sequence[ast.AST], refs: list[Ref]) -> list[Ref]:
    """`refs` minus what exists only because the file patches it (see the module docstring)."""
    patch_only = _patch_only_names(nodes)
    return [
        ref
        for ref in refs
        if not (ref.kind == "string-target" and ref.via.rsplit(".", 1)[-1] in _PATCH_CALLEES)
        and not (ref.kind == "import" and ref.names and all(n in patch_only for n in ref.names))
    ]


def placement_references(
    tree: ast.AST, index: ModuleIndex, nodes: Sequence[ast.AST] | None = None, rel_path: str = ""
) -> tuple[list[Ref], bool]:
    """(the references that decide the home, whether the patch-evidence fallback applied).

    Fallback: a file whose every strong reference is patch evidence keeps all of them.
    """
    refs = collect_references(tree, index, rel_path)
    pruned = without_patch_evidence(list(ast.walk(tree)) if nodes is None else nodes, refs)
    if _has_strong(refs) and not _has_strong(pruned):
        return refs, True
    return pruned, False


# --------------------------------------------------------------------------- placement


@dataclass(frozen=True)
class Placement:
    """The package a test file belongs to: `home` is a code directory such as `base/host`."""

    home: str | None
    unit: str | None = None
    fallback: bool = False  # the patch-evidence fallback applied
    ambiguous: bool = False  # no unit could legally import all the others


def _pick_unit(
    units: list[str], graph: UnitGraph, weight: collections.Counter[str], stem: str, refs: list[Ref]
) -> tuple[str, bool]:
    """(home unit, ambiguous)."""
    unique = sorted(set(units))
    if len(unique) == 1:
        return unique[0], False
    maximal = [u for u in unique if all(graph.can_import(u, v) for v in unique if v != u)]
    if len(maximal) == 1:
        return maximal[0], False
    if maximal:  # mutual reachability: break the tie by the heavier source edge balance

        def balance(unit: str) -> tuple[int, int]:
            out = sum(graph.empirical[unit, v] for v in unique if v != unit)
            into = sum(graph.empirical[v, unit] for v in unique if v != unit)
            return out - into, weight[unit]

        return max(maximal, key=balance), False
    return _ambiguous_pick(unique, graph, weight, stem, refs), True


def _ambiguous_pick(
    unique: list[str],
    graph: UnitGraph,
    weight: collections.Counter[str],
    stem: str,
    refs: list[Ref],
) -> str:
    def fits(unit: str) -> bool:
        return not any(graph.blocked(unit, other) for other in unique if other != unit)

    def rank(unit: str) -> tuple[int, int, int]:
        others = [v for v in unique if v != unit]
        return (
            sum(graph.can_import(unit, v) for v in others),
            sum(graph.empirical[unit, v] for v in others),
            weight[unit],
        )

    pool = [unit for unit in unique if fits(unit)] or unique
    named = _unit_named_after(stem, unique, graph, refs)
    return named or max(pool, key=rank)  # max: the first maximum, as a stable sort would give


def _unit_named_after(
    stem: str, unique: list[str], graph: UnitGraph, refs: list[Ref]
) -> str | None:
    """The unit of the referenced module the file is named after (`test_<module>.py`)."""
    for ref in refs:
        legal = ref.unit in unique and ref.kind in STRONG_KINDS
        if legal and not any(graph.blocked(ref.unit, v) for v in unique if v != ref.unit):
            leaf = ref.module.split(".")[-1].lstrip("_")
            named = stem == leaf or stem.endswith("_" + leaf) or stem.startswith(leaf + "_")
            if len(leaf) >= 4 and named:
                return ref.unit
    return None


def common_dir(dirs: list[str]) -> str:
    """The nearest common ancestor directory of `dirs`."""
    common: list[str] = []
    for parts in zip(*(d.split("/") for d in dirs), strict=False):
        if len(set(parts)) != 1:
            break
        common.append(parts[0])
    return "/".join(common)


def is_top_level(rel_path: str) -> bool:
    """A file that stays at the top level by policy."""
    return rel_path in TOP_LEVEL_FILES or rel_path.startswith(TOP_LEVEL_PREFIXES)


def _inside(directory: str, package: str) -> bool:
    return directory == package or directory.startswith(package + "/")


def _chain(directories: Iterable[str], bound: str) -> set[str]:
    """`bound` and every package below it on an ancestor chain of one of `directories`."""
    floor = bound.count("/")
    parts = [directory.split("/") for directory in directories]
    return {
        "/".join(chain[:depth]) for chain in parts for depth in range(floor + 1, len(chain) + 1)
    }


def _serves(index: ModuleIndex, package: str, dirs: dict[str, str]) -> bool:
    """Does `package` hold or directly import every module (`dirs`: module -> its directory)?"""
    return all(
        _inside(directory, package) or package in index.importers.get(module, ())
        for module, directory in dirs.items()
    )


def _home_dir(index: ModuleIndex, unit: str, modules: list[str]) -> str:
    """The deepest package of the unit that holds or directly depends on every module.

    The bound is the nearest common ancestor directory of the modules; a package on a module's
    ancestor chain below it qualifies when each module is in its subtree or imported by its
    non-test code. One deepest package is the home; none (a dependency cycle) leaves the bound.
    """
    dirs = {module: index.dir_of(module) for module in sorted(set(modules))}
    bound = common_dir(list(dirs.values()))
    root = unit_root(unit)
    if not (index.repo_root / root).is_dir():  # a loose module file such as services/pidfile.py
        root = str(Path(root).parent)
    if not bound.startswith(root):
        return root
    fits = [package for package in _chain(dirs.values(), bound) if _serves(index, package, dirs)]
    deepest = [
        package for package in fits if not any(o != package and _inside(o, package) for o in fits)
    ]
    return deepest[0] if len(deepest) == 1 else bound


def place(
    rel_path: str, tree: ast.AST, index: ModuleIndex, nodes: Sequence[ast.AST] | None = None
) -> Placement:
    """Where the file at `rel_path` (repo-relative, POSIX) belongs.

    `nodes` is `list(ast.walk(tree))` when the caller already has it (saves one traversal).
    """
    if is_top_level(rel_path):
        return Placement(None)
    found, fallback = placement_references(tree, index, nodes, rel_path)
    refs = list(found)
    basis = [ref for ref in refs if ref.kind in STRONG_KINDS] or refs  # loose evidence last
    if not basis:
        return Placement(None)
    weight = collections.Counter(ref.unit for ref in refs)
    stem = Path(rel_path).name.removeprefix("test_").removesuffix(".py")
    graph = unit_graph(index.repo_root)
    unit, ambiguous = _pick_unit(sorted({ref.unit for ref in basis}), graph, weight, stem, refs)
    home = _home_dir(index, unit, [ref.module for ref in basis if ref.unit == unit])
    return Placement(home, unit, fallback, ambiguous)
