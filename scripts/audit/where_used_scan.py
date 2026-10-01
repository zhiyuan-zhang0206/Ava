"""The reference finder behind `where_used.py`: what a target is and where it is named.

`load_repo` reads the repository once, `resolve` turns a symbol, module, package or
path into a `Target`, and `file_hits` lists every reference to it in one file:

- Python files are read as an AST for imports (absolute, relative, deferred in a
  function, `from x import y`, wildcard) and, for a symbol, for attribute use through an
  imported module and object-form patches (`monkeypatch.setattr(mod, "name", ...)`); the
  remaining matches of the textual forms are classified by what surrounds them (patch
  target, dynamic import, `-m` entry point, string, docstring, comment). Plain code is
  skipped: an import already accounts for it.
- Every other file is matched on the textual forms; markdown additionally resolves
  relative links and matches a symbol inside inline code. A file whose name is unique in
  the repository is also matched by that bare name (markdown) or quoted name (anywhere:
  `Path(...) / "scripts" / "x.py"`).
- A symbol that a package `__init__` imports at its top level is also importable from the
  package (`find_doors`): importers of that door count as importers of the symbol.

Grouping and presentation are `where_used.py`; this module only finds hits.
"""

from __future__ import annotations

import ast
import posixpath
import re
import subprocess
from bisect import bisect_left, bisect_right
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

_CLIP = 110
_GENERATED = frozenset({".test_durations"})
_DOC_SUFFIXES = (".md", ".mdx", ".rst")
_DYNAMIC_CALLS = frozenset(
    {"import_module", "importorskip", "__import__", "find_spec", "run_module", "resolve_name"}
)
_STRING_RANK = {"string": 1, "docstring": 2, "dynamic import": 3, "patch target": 3}

_NEWLINE = re.compile("\n")
_LINK = re.compile(r"\]\(\s*<?([^)\s>#]+)|\[\[([^\]|#\s]+)")
_SCHEME = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*:")
_MODULE_ENTRY = re.compile(r"""-m(?:\s+|["']\s*,\s*["'])$""")
_WORD_DASH = "_-"


@dataclass(frozen=True)
class Hit:
    """One reference: where it is, what kind it is, and the line it sits on."""

    path: str
    line: int  # 0 for a test matched by name only
    kind: str
    text: str


@dataclass(frozen=True)
class Door:
    """A package `__init__` that imports the symbol at its top level, so it is also importable
    from the package under `name`."""

    module: str
    name: str
    where: str  # path:line


def _dotted_left(text: str, start: int) -> bool:
    """A dotted name must not be the tail of a longer one (`x.a.b` is not `a.b`)."""
    return start == 0 or not (text[start - 1].isalnum() or text[start - 1] in "_.")


def _path_left(text: str, start: int) -> bool:
    """A repo-root path must not be the tail of another path (`./a/b.py` still counts)."""
    if start == 0:
        return True
    before = text[start - 1]
    if before.isalnum() or before in _WORD_DASH:
        return False
    if before == "/" and start > 1:
        return not (text[start - 2].isalnum() or text[start - 2] in _WORD_DASH)
    return True


def _name_left(text: str, start: int) -> bool:
    """A file name must not be part of a longer name or path."""
    return start == 0 or not (text[start - 1].isalnum() or text[start - 1] in "_-./")


def _anywhere(_text: str, _start: int) -> bool:
    return True


@dataclass(frozen=True)
class Form:
    """A textual form of a reference. The regex starts with a literal so the engine can
    search for it; `left_ok` then checks what precedes the match."""

    regex: re.Pattern[str]
    left_ok: Callable[[str, int], bool]


@dataclass(frozen=True)
class Repo:
    """Every readable tracked or unignored text file, and which of them are modules."""

    texts: dict[str, str]
    modules: dict[str, str]  # dotted name -> its file (a directory for a namespace package)
    by_path: dict[str, str]  # a module file or package directory -> dotted name


@dataclass(frozen=True)
class Target:
    """What the caller asked about, resolved against the repository."""

    raw: str
    kind: str  # symbol | module | package | file | directory
    path: str  # defining file, package directory, or the path itself
    module: str = ""
    name: str = ""
    defined_at: str = ""

    @property
    def is_dir(self) -> bool:
        return self.kind in ("package", "directory")


@dataclass(frozen=True)
class Matcher:
    """The forms a reference to the target can take, compiled once per target."""

    target: Target
    pairs: tuple[tuple[str, str], ...]  # symbol targets: (module, name), doors included
    forms: tuple[Form, ...]
    about: tuple[Form, ...]  # symbol targets: how a document names the symbol's module
    bare: Form | None  # inline-code mention of a symbol, markdown only
    file_mention: Form | None  # unique file name, markdown only
    needles: tuple[str, ...]  # every textual reference contains one of these
    names: tuple[str, ...]  # symbol targets: the names it is bound to, doors included
    link_path: str  # markdown links to this path (or below it, for a directory) count

    @property
    def symbol(self) -> bool:
        return bool(self.target.name)


# ---------------------------------------------------------------- repository


def load_repo(root: Path) -> Repo:
    """Read every tracked (and unignored untracked) text file once."""
    listing = subprocess.check_output(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"], cwd=root
    )
    texts: dict[str, str] = {}
    for name in listing.decode().split("\0"):
        path = root / name
        if not name or name in _GENERATED or path.is_symlink() or not path.is_file():
            continue
        data = path.read_bytes()
        if b"\0" in data[:8192]:
            continue
        try:
            texts[name] = data.decode("utf-8")
        except UnicodeDecodeError:
            continue
    return index_repo(texts)


def _dotted(path: str) -> str | None:
    """The dotted name of a path that can be imported, else None."""
    if not path.endswith(".py"):
        return None
    parts = path[:-3].split("/")
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts) if parts and all(part.isidentifier() for part in parts) else None


def index_repo(texts: dict[str, str]) -> Repo:
    """A `Repo` over the given path -> text mapping: which paths are importable modules."""
    modules: dict[str, str] = {}
    by_path: dict[str, str] = {}
    for name in texts:
        dotted = _dotted(name)
        if dotted is not None:
            modules[dotted] = name
            by_path[name] = dotted
            if name.endswith("/__init__.py"):
                by_path[posixpath.dirname(name)] = dotted
    for name in tuple(by_path):  # directories without `__init__.py` are namespace packages
        parent = posixpath.dirname(name)
        while parent and parent not in by_path:
            dotted = _dotted(parent + ".py")
            if dotted is not None:
                modules.setdefault(dotted, parent)
                by_path[parent] = dotted
            parent = posixpath.dirname(parent)
    return Repo(texts, modules, by_path)


def _parse(text: str) -> ast.Module | None:
    try:
        return ast.parse(text)
    except (SyntaxError, ValueError):
        return None


# ------------------------------------------------------------ target resolution


def _split(raw: str) -> tuple[str, bool, str]:
    """`spec`, whether a `:name` / `::name` follows, and the name."""
    spec, sep, name = raw.partition("::")
    if not sep:
        spec, sep, name = raw.partition(":")
    return (posixpath.normpath(spec) if "/" in spec else spec), bool(sep), name


def resolve(raw: str, repo: Repo) -> Target:
    """Resolve a symbol, module, package, file or directory; ValueError if it is none."""
    spec, has_name, name = _split(raw)
    module = repo.by_path.get(spec) or (spec if spec in repo.modules else None)
    if has_name:
        if module is None:
            raise ValueError(f"{raw!r}: {spec!r} is not a module or Python file of this repository")
        return _symbol(repo, module, name, raw)
    return _module(repo, module, raw) if module is not None else _other(repo, spec, raw)


def _other(repo: Repo, spec: str, raw: str) -> Target:
    """`pkg.mod.name`, a tracked file or a directory."""
    parent, _, leaf = spec.rpartition(".")
    if parent in repo.modules:
        return _symbol(repo, parent, leaf, raw)
    if spec in repo.texts:
        return Target(raw, "file", spec)
    if any(path.startswith(spec + "/") for path in repo.texts):
        return Target(raw, "directory", spec)
    raise ValueError(f"{raw!r} is not a symbol, module, tracked file or directory here")


def _module(repo: Repo, module: str, raw: str) -> Target:
    file = repo.modules[module]
    if file.endswith("/__init__.py"):
        return Target(raw, "package", file.rsplit("/", 1)[0], module)
    return Target(raw, "module" if file.endswith(".py") else "package", file, module)


def _symbol(repo: Repo, module: str, name: str, raw: str) -> Target:
    file = repo.modules[module]
    if not file.endswith(".py"):
        raise ValueError(f"{raw!r}: {module} is a namespace package, it defines no names")
    if "." in name:
        raise ValueError(
            f"{raw!r}: name the class or function, not a member ({name.partition('.')[0]!r})"
        )
    tree = _parse(repo.texts[file])
    lines = [line for bound, line in _bindings(tree) if bound == name] if tree else []
    if not lines:
        raise ValueError(
            f"{raw!r}: {module} ({file}) binds no top-level name {name!r} "
            "(names added at runtime are not supported)"
        )
    return Target(raw, "symbol", file, module, name, f"{file}:{min(lines)}")


def _bound_names(node: ast.stmt) -> list[str]:
    match node:
        case ast.FunctionDef(name=name) | ast.AsyncFunctionDef(name=name) | ast.ClassDef(name=name):
            return [name]
        case ast.TypeAlias(name=ast.Name(id=name)) | ast.AnnAssign(target=ast.Name(id=name)):
            return [name]
        case ast.Assign(targets=targets):
            return [target.id for target in targets if isinstance(target, ast.Name)]
        case ast.Import(names=names) | ast.ImportFrom(names=names):
            return [alias.asname or alias.name.split(".")[0] for alias in names]
        case _:
            return []


def _bindings(tree: ast.Module) -> Iterator[tuple[str, int]]:
    """Names bound at the top level of a module: def, class, assignment, import."""
    for node, _ in _statements(tree.body, nested=False):
        for name in _bound_names(node):
            yield name, node.lineno


def _inner_bodies(stmt: ast.stmt) -> list[Sequence[ast.stmt]]:
    """The statement lists nested directly in a compound statement."""
    bodies: list[Sequence[ast.stmt]] = [
        getattr(stmt, field, ()) for field in ("body", "orelse", "finalbody")
    ]
    bodies += [handler.body for handler in getattr(stmt, "handlers", ())]
    bodies += [case.body for case in getattr(stmt, "cases", ())]
    return bodies


def _statements(
    body: Sequence[ast.stmt], *, nested: bool, depth: int = 0
) -> Iterator[tuple[ast.stmt, int]]:
    """Statements with their function depth. Expressions are never entered, which is what
    makes this cheap; `nested=False` stays out of function and class bodies."""
    for stmt in body:
        yield stmt, depth
        function = isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef)
        if nested or not (function or isinstance(stmt, ast.ClassDef)):
            for found in _inner_bodies(stmt):
                yield from _statements(found, nested=nested, depth=depth + 1 if function else depth)


# ----------------------------------------------------------------- doors, matcher


def _resolve_from(node: ast.ImportFrom, current: str | None, *, is_pkg: bool) -> str | None:
    """The dotted module a `from ... import` names, relative imports resolved."""
    if node.level == 0:
        return node.module
    if current is None:
        return None
    parts = current.split(".")
    if not is_pkg:
        parts = parts[:-1]
    drop = node.level - 1
    if drop > len(parts):
        return None
    parts = parts[: len(parts) - drop]
    if node.module:
        parts.append(node.module)
    return ".".join(parts) or None


def find_doors(repo: Repo, module: str, name: str) -> list[Door]:
    """Package `__init__` files that import the symbol at their top level, followed in turn."""
    inits = {p: m for p, m in repo.by_path.items() if p.endswith("/__init__.py")}
    doors: list[Door] = []
    seen = {(module, name)}
    queue = [(module, name)]
    while queue:
        origin, bound = queue.pop()
        for path, package in inits.items():
            found = _reexport(repo.texts[path], package, origin, bound)
            if found is not None and (package, found[1]) not in seen:
                seen.add((package, found[1]))
                queue.append((package, found[1]))
                doors.append(Door(package, found[1], f"{path}:{found[0]}"))
    return doors


def _reexport(text: str, package: str, origin: str, name: str) -> tuple[int, str] | None:
    """(line, bound name) of the `from <origin> import <name>` in a package `__init__`."""
    tree = _parse(text) if name in text else None
    if tree is None:
        return None
    for node, _ in _statements(tree.body, nested=False):
        if isinstance(node, ast.ImportFrom) and _resolve_from(node, package, is_pkg=True) == origin:
            for alias in node.names:
                if alias.name == name:
                    return node.lineno, alias.asname or name
    return None


def _dotted_form(text: str) -> Form:
    return Form(re.compile(text), _dotted_left)


def _path_form(text: str, tail: str = r"(?![\w-])(?!\.\w)") -> Form:
    return Form(re.compile(re.escape(text) + tail), _path_left)


def _forms(repo: Repo, target: Target, pairs: Sequence[tuple[str, str]]) -> list[Form]:
    if target.name:
        forms: list[Form] = []
        for module, name in pairs:
            tail = re.escape(name)
            forms.append(_dotted_form(rf"{re.escape(module)}[.:]{tail}(?!\w)"))
            forms.append(_path_form(repo.modules[module], rf"::?{tail}(?!\w)"))
        return forms
    if target.module:
        forms = [_path_form(target.path) if target.is_dir else _path_form(target.path, r"(?!\w)")]
        if "." in target.module:  # a bare top-level name matches ordinary words
            forms.append(_dotted_form(rf"{re.escape(target.module)}(?!\w)"))
        return forms
    return [_path_form(target.path)]


def _module_forms(repo: Repo, target: Target) -> list[Form]:
    """How a document says it is about the symbol's module: its path or its dotted name."""
    forms = [_path_form(repo.modules[target.module], r"(?!\w)")]
    if "." in target.module:
        forms.append(_dotted_form(rf"{re.escape(target.module)}(?!\w)"))
    return forms


def _unique_file_name(repo: Repo, target: Target) -> str | None:
    """The file's name when no other file in the repository has it, so a bare mention of the
    name is unambiguous."""
    base = posixpath.basename(target.path)
    if target.kind not in ("module", "file") or base == "__init__.py":
        return None
    named = sum(1 for path in repo.texts if path == base or path.endswith("/" + base))
    return base if named == 1 else None


def _name_forms(name: str) -> list[Form]:
    """A quoted file name: how code locates a file (`Path(...) / "scripts" / "x.py"`)."""
    return [Form(re.compile(f"{quote}{re.escape(name)}{quote}"), _anywhere) for quote in "\"'"]


def build_matcher(repo: Repo, target: Target, doors: Sequence[Door] = ()) -> Matcher:
    pairs: tuple[tuple[str, str], ...] = ()
    bare: Form | None = None
    if target.name:
        pairs = ((target.module, target.name), *((d.module, d.name) for d in doors))
        names = tuple(dict.fromkeys(name for _, name in pairs))
        needles = (*names, "import *")  # a wildcard import may use the symbol
        inline = rf"`[^`\n]*(?<!\w){re.escape(target.name)}(?!\w)[^`\n]*`"
        bare, link_path = Form(re.compile(inline), _anywhere), ""
    else:
        leaf = (
            target.module.rsplit(".", 1)[-1] if target.module else posixpath.basename(target.path)
        )
        names, needles, link_path = (), (leaf,), target.path
    forms = _forms(repo, target, pairs)
    unique = _unique_file_name(repo, target)
    mention = Form(re.compile(rf"{re.escape(unique)}(?!\w)"), _name_left) if unique else None
    forms += _name_forms(unique) if unique else []
    about = tuple(_module_forms(repo, target)) if target.name else ()
    return Matcher(target, pairs, tuple(forms), about, bare, mention, needles, names, link_path)


# ----------------------------------------------------------- Python scanning


def _under(module: str, target: str) -> bool:
    return module == target or module.startswith(target + ".")


def _chain(node: ast.Attribute) -> tuple[str, list[str]] | None:
    """`a.b.c` -> ("a", ["b", "c"]) when the chain is rooted at a plain name."""
    parts: list[str] = []
    current: ast.expr = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    return (current.id, parts[::-1]) if isinstance(current, ast.Name) else None


class _Imports:
    """Import statements that reach the target and, for a symbol, attribute use of it."""

    def __init__(self, matcher: Matcher, repo: Repo, path: str) -> None:
        self.matcher = matcher
        self.repo = repo
        self.current = repo.by_path.get(path)
        self.is_init = path.endswith("__init__.py")
        self.hits: list[tuple[int, str]] = []
        self._aliases: dict[str, str] = {}

    def run(self, tree: ast.Module) -> list[tuple[int, str]]:
        for node, depth in _statements(tree.body, nested=True):
            if isinstance(node, ast.Import):
                self._import(node, deferred=depth > 0)
            elif isinstance(node, ast.ImportFrom):
                self._import_from(node, deferred=depth > 0)
        if self._aliases:
            self._uses(tree)
        return self.hits

    def _hit(self, line: int, kind: str, *, deferred: bool) -> None:
        inside = deferred and kind in ("import", "re-export")
        self.hits.append((line, "import in function" if inside else kind))

    def _import(self, node: ast.Import, *, deferred: bool) -> None:
        for alias in node.names:
            if self.matcher.symbol:
                root = alias.name.split(".")[0]
                self._aliases[alias.asname or root] = alias.name if alias.asname else root
            elif _under(alias.name, self.matcher.target.module):
                self._hit(node.lineno, "import", deferred=deferred)

    def _import_from(self, node: ast.ImportFrom, *, deferred: bool) -> None:
        base = _resolve_from(node, self.current, is_pkg=self.is_init)
        if base is None:
            return
        if self.matcher.symbol:
            self._symbol_from(node, base, deferred=deferred)
            return
        module = self.matcher.target.module
        if _under(base, module):
            star = any(alias.name == "*" for alias in node.names)
            self._hit(node.lineno, "wildcard import" if star else "import", deferred=deferred)
        elif any(_under(f"{base}.{alias.name}", module) for alias in node.names):
            self._hit(node.lineno, "import", deferred=deferred)

    def _symbol_from(self, node: ast.ImportFrom, base: str, *, deferred: bool) -> None:
        for alias in node.names:
            full = f"{base}.{alias.name}"
            if full in self.repo.modules:
                self._aliases[alias.asname or alias.name] = full
            kind = self._symbol_kind(base, alias.name)
            if kind:
                self._hit(node.lineno, kind, deferred=deferred)

    def _symbol_kind(self, base: str, imported: str) -> str | None:
        """What `from <base> import <imported>` does to the symbol, if anything."""
        for module, name in self.matcher.pairs:
            if base != module:
                continue
            if imported == "*":
                return "wildcard import"
            if imported == name:
                return "re-export" if self.is_init else "import"
        return None

    def _dotted(self, node: ast.expr) -> str | None:
        """The dotted module an expression names through the file's imports, if it does."""
        if isinstance(node, ast.Name):
            return self._aliases.get(node.id)
        chain = _chain(node) if isinstance(node, ast.Attribute) else None
        base = self._aliases.get(chain[0]) if chain else None
        return ".".join([base, *chain[1]]) if chain and base else None

    def _uses(self, tree: ast.Module) -> None:
        """Use of the symbol through an imported module: `mod.name`, and the object form of a
        patch, `monkeypatch.setattr(mod, "name", ...)` / `patch.object(mod, "name")`."""
        wanted = [f"{module}.{name}" for module, name in self.matcher.pairs]
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                dotted = self._dotted(node)
                if dotted and any(_under(dotted, full) for full in wanted):
                    self.hits.append((node.lineno, "attribute"))
            elif isinstance(node, ast.Call) and self._patches(node, wanted):
                self.hits.append((node.lineno, "patch target"))

    def _patches(self, call: ast.Call, wanted: Sequence[str]) -> bool:
        callee = _callee(call.func)
        if callee.rsplit(".", 1)[-1] not in ("setattr", "delattr") and not callee.endswith(
            "patch.object"
        ):
            return False
        name = _text_constant(call.args[1]) if len(call.args) > 1 else None
        module = self._dotted(call.args[0]) if name is not None else None
        return module is not None and name is not None and f"{module}.{name.value}" in wanted


def _may_import(matcher: Matcher, text: str) -> bool:
    """Cheap precheck: could this file's imports reach the target at all?"""
    if matcher.symbol:
        return any(name in text for name in matcher.needles)
    module = matcher.target.module
    parent = module.rsplit(".", 1)[0] if "." in module else module
    return matcher.needles[0] in text and (
        module in text or f"from {parent} import" in text or "from ." in text
    )


def _callee(node: ast.expr) -> str:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def _call_kind(call: ast.Call) -> str | None:
    callee = _callee(call.func)
    last = callee.rsplit(".", 1)[-1]
    if last in ("setattr", "delattr") or callee == "patch":
        return "patch target"
    if callee.endswith((".patch", "patch.dict", "patch.multiple")):
        return "patch target"
    return "dynamic import" if last in _DYNAMIC_CALLS else None


def _mark(contexts: dict[int, str], node: ast.Constant, kind: str) -> None:
    for line in range(node.lineno, (node.end_lineno or node.lineno) + 1):
        if _STRING_RANK[kind] >= _STRING_RANK.get(contexts.get(line, ""), 0):
            contexts[line] = kind


def _covers(node: ast.AST, lines: Sequence[int]) -> bool:
    """Whether the node spans any of the (sorted) lines; nodes without a position do."""
    start: int | None = getattr(node, "lineno", None)
    if start is None:
        return True
    decorators: list[ast.expr] = getattr(node, "decorator_list", [])
    start = min([start, *(d.lineno for d in decorators)])  # a `def` line follows its decorators
    end: int = getattr(node, "end_lineno", None) or start
    index = bisect_left(lines, start)
    return index < len(lines) and lines[index] <= end


def _text_constant(node: ast.AST | None) -> ast.Constant | None:
    return node if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _mark_call(contexts: dict[int, str], call: ast.Call) -> None:
    kind = _call_kind(call)
    first = _text_constant(call.args[0]) if call.args else None
    if kind and first is not None:
        _mark(contexts, first, kind)


def _mark_docstring(
    contexts: dict[int, str],
    scope: ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef,
) -> None:
    head = scope.body[0] if scope.body else None
    doc = _text_constant(head.value) if isinstance(head, ast.Expr) else None
    if doc is not None:
        _mark(contexts, doc, "docstring")


def _mark_node(contexts: dict[int, str], node: ast.AST) -> None:
    text = _text_constant(node)
    if text is not None:
        _mark(contexts, text, "string")
    elif isinstance(node, ast.Call):
        _mark_call(contexts, node)
    elif isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
        _mark_docstring(contexts, node)


def _string_contexts(tree: ast.Module, lines: Sequence[int]) -> dict[int, str]:
    """For the given sorted lines: what string sits there (a patch target, a dynamic import,
    a docstring, a plain string). Subtrees away from those lines are not entered."""
    contexts: dict[int, str] = {}
    stack: list[ast.AST] = [tree]
    while stack:
        node = stack.pop()
        if _covers(node, lines):
            _mark_node(contexts, node)
            stack.extend(ast.iter_child_nodes(node))
    return contexts


def _python_kind(contexts: dict[int, str] | None, row: str, line: int, col: int) -> str | None:
    """What a textual match on a line of Python is; None for plain code (imports cover it)."""
    if contexts is None:  # the file does not parse: keep the match, kind unknown
        return "reference"
    kind = contexts.get(line)
    if kind is None:
        return "comment" if "#" in row[:col] else None
    return "module entry" if kind == "string" and _MODULE_ENTRY.search(row[:col]) else kind


def _python_hits(path: str, text: str, matcher: Matcher, repo: Repo) -> list[Hit]:
    rows = text.split("\n")
    matches = list(_find(text, matcher.forms))
    importable = _may_import(matcher, text)
    tree = _parse(text) if matches or importable else None
    hits: list[Hit] = []
    if tree is not None and importable:
        found = _Imports(matcher, repo, path).run(tree)
        hits.extend(Hit(path, line, kind, _clip(rows[line - 1])) for line, kind in found)
        imported = {line for line, _ in found}  # the import hit already covers its line
        matches = [(line, col) for line, col in matches if line not in imported]
    return hits + _classified(path, rows, matches, tree)


def _classified(
    path: str, rows: list[str], matches: list[tuple[int, int]], tree: ast.Module | None
) -> list[Hit]:
    """Textual matches of a Python file, each told apart by what surrounds it."""
    if not matches:
        return []
    wanted = sorted({line for line, _ in matches})
    contexts = _string_contexts(tree, wanted) if tree is not None else None
    kinds = [
        (line, col, _python_kind(contexts, rows[line - 1], line, col)) for line, col in matches
    ]
    return [Hit(path, line, kind, _clip(rows[line - 1], col)) for line, col, kind in kinds if kind]


# --------------------------------------------------------------- text scanning


def _clip(row: str, col: int = 0) -> str:
    """The line, stripped; a long one is cut to a window around the match column."""
    if len(row.strip()) <= _CLIP:
        return row.strip()
    start = max(0, col - _CLIP // 3)
    window = row[start : start + _CLIP].strip()
    return ("..." if start else "") + window + "..."


def _find(text: str, forms: Iterable[Form]) -> Iterator[tuple[int, int]]:
    """(line, column) of every match; lines count from 1, columns from 0."""
    starts: list[int] | None = None
    for form in forms:
        for found in form.regex.finditer(text):
            if not form.left_ok(text, found.start()):
                continue
            if starts is None:
                starts = [0, *(m.end() for m in _NEWLINE.finditer(text))]
            line = bisect_right(starts, found.start())
            yield line, found.start() - starts[line - 1]


def _links(text: str) -> Iterator[tuple[int, str]]:
    for found in _LINK.finditer(text):
        link = found.group(1) or found.group(2)
        if not _SCHEME.match(link):
            yield text.count("\n", 0, found.start()) + 1, link


def is_doc_file(path: str) -> bool:
    return path.endswith(_DOC_SUFFIXES)


def _link_hits(path: str, text: str, rows: list[str], matcher: Matcher) -> list[Hit]:
    """Markdown links that resolve (relative to the file) to the target."""
    folder = posixpath.dirname(path)
    hits: list[Hit] = []
    for line, link in _links(text):
        resolved = link.lstrip("/") if link.startswith("/") else posixpath.join(folder, link)
        resolved = posixpath.normpath(resolved)
        inside = matcher.target.is_dir and resolved.startswith(matcher.link_path + "/")
        if resolved == matcher.link_path or inside:
            hits.append(Hit(path, line, "link", _clip(rows[line - 1])))
    return hits


def _about_symbol(path: str, text: str, matcher: Matcher) -> bool:
    """Whether a bare symbol name in this markdown file can be taken to mean the symbol: the
    name is not an ordinary word (it has an underscore or a capital), or the file is about
    the symbol's module (it names the module, or sits in the module's directory)."""
    name = matcher.target.name
    if "_" in name or name != name.lower():
        return True
    home = posixpath.dirname(matcher.target.path)
    return bool(home and path.startswith(home + "/")) or any(
        next(_find(text, [form]), None) for form in matcher.about
    )


def _doc_hits(path: str, text: str, rows: list[str], matcher: Matcher) -> list[Hit]:
    """Markdown-only forms: inline-code symbol names, unique file names and resolved links."""
    bare = matcher.bare if matcher.bare and _about_symbol(path, text, matcher) else None
    extra = ((bare, "mention (bare name)"), (matcher.file_mention, "mention (file name)"))
    hits = [
        Hit(path, line, kind, _clip(rows[line - 1], col))
        for form, kind in extra
        if form is not None
        for line, col in _find(text, [form])
    ]
    return hits + _link_hits(path, text, rows, matcher) if matcher.link_path else hits


def _plain_hits(path: str, text: str, matcher: Matcher) -> list[Hit]:
    rows = text.split("\n")
    doc = is_doc_file(path)
    hits: list[Hit] = []
    for line, col in _find(text, matcher.forms):
        entry = _MODULE_ENTRY.search(rows[line - 1][:col])
        kind = "module entry" if entry else ("mention" if doc else "reference")
        hits.append(Hit(path, line, kind, _clip(rows[line - 1], col)))
    return [*_doc_hits(path, text, rows, matcher), *hits] if doc else hits


def file_hits(path: str, text: str, matcher: Matcher, repo: Repo) -> list[Hit]:
    """Every reference to the target in one file."""
    if not any(needle in text for needle in matcher.needles):
        return []
    if path.endswith(".py"):
        return _python_hits(path, text, matcher, repo)
    return _plain_hits(path, text, matcher)
