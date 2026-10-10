"""Runtime dependency closure for backend test selection, without executing code."""

from __future__ import annotations

import ast
import io
import os
import subprocess
import tarfile
import tempfile
from collections import deque
from collections.abc import Generator, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from scripts.structure.imports.facts import FactKind, Unknown, collect
from scripts.structure.imports.fixture_scopes import declarations
from scripts.structure.placement import CODE_TOPS, ModuleIndex

_TOPS = tuple(dict.fromkeys((*CODE_TOPS, "tests")))
_SKIPPED = frozenset({"node_modules", "__pycache__"})
_MODULE_FACTS = frozenset(
    {
        FactKind.IMPORT,
        FactKind.DYNAMIC_IMPORT,
        FactKind.EMBEDDED_IMPORT,
        FactKind.PYTHON_MODULE,
    }
)


# The path-scoped fixture readers load the TOML tables and the fixture modules they
# name. `_fixture_edges` binds each declared module and its TOML source to exactly
# the tests in its scope, so those runtime-chosen inputs are already modeled here;
# keeping them as unknown evidence would make every test depend on every table.
_MODELED_FIXTURE_READERS = {
    "tests/fixtures/path_scopes.py": FactKind.DYNAMIC_IMPORT,
    "scripts/structure/imports/fixture_scopes.py": FactKind.RESOURCE,
}


@dataclass(frozen=True)
class Impact:
    """Unpruned facts projected onto collectable tests, plus incomplete evidence."""

    tests_by_input: dict[str, set[str]]
    unknown: tuple[Unknown, ...]


def _python_files(root: Path) -> Iterator[Path]:
    for top in _TOPS:
        directory = root / top
        if directory.is_dir():
            for current, dirs, files in os.walk(directory):
                dirs[:] = sorted(
                    part for part in dirs if not part.startswith(".") and part not in _SKIPPED
                )
                for name in sorted(files):
                    if name.endswith(".py") and not name.startswith("."):
                        yield Path(current) / name
    conftest = root / "conftest.py"
    if conftest.is_file():
        yield conftest


def module_files(module: str, index: ModuleIndex) -> set[str]:
    """Importing a module also executes its concrete package initializers."""
    parts = module.split(".")
    files = {
        file
        for end in range(1, len(parts))
        if (file := index.file(".".join(parts[:end]))) is not None and file.endswith("/__init__.py")
    }
    if (file := index.file(module)) is not None:
        files.add(file)
    return files


def _scope_tests(scope: str, tests: frozenset[str]) -> set[str]:
    return {test for test in tests if test == scope or test.startswith(f"{scope}/")}


def _fixture_edges(
    root: Path,
    tests: frozenset[str],
    index: ModuleIndex,
    incomplete: dict[str, tuple[Unknown, ...]],
) -> dict[str, set[str]]:
    """Each test depends on conftests, fixture modules and their declaration inputs."""
    edges: dict[str, set[str]] = {test: set() for test in tests}
    for test in tests:
        edges[test].update(module_files(test.removesuffix(".py").replace("/", "."), index))
        for parent in (Path(test).parent, *Path(test).parent.parents):
            rel = (parent / "conftest.py").as_posix()
            if (root / rel).is_file():
                edges[test].add(rel)
    for declaration in declarations(root):
        files = module_files(declaration.module, index) | {declaration.source}
        if (
            declaration.module.split(".", maxsplit=1)[0] in _TOPS
            and index.kind(declaration.module) is None
        ):
            incomplete[declaration.source] = (
                *incomplete.get(declaration.source, ()),
                Unknown(
                    path=declaration.source,
                    line=0,
                    expression=declaration.module,
                    reason="Declared first-party fixture module is missing",
                    kind=FactKind.IMPORT,
                ),
            )
        for path in declaration.paths:
            for test in _scope_tests(path, tests):
                edges[test].update(files)
    return edges


def plugin_modules(tree: ast.Module) -> tuple[str, ...]:
    """The literal fixture-plugin declaration, shared by classification and runtime edges."""
    modules: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets, expression = node.targets, node.value
        elif isinstance(node, ast.AnnAssign):
            targets, expression = [node.target], node.value
        else:
            continue
        if not any(
            isinstance(target, ast.Name) and target.id == "pytest_plugins" for target in targets
        ):
            continue
        value: object = ast.literal_eval(expression) if expression is not None else None
        if not isinstance(value, (list, tuple)):
            raise TypeError("pytest_plugins must be a literal sequence of module-name strings")
        items = cast(list[object] | tuple[object, ...], value)
        if not all(isinstance(item, str) for item in items):
            raise TypeError("pytest_plugins must be a literal sequence of module-name strings")
        modules.extend(cast(list[str] | tuple[str, ...], items))
    return tuple(modules)


def build_impact(root: Path, tests: frozenset[str]) -> Impact:
    """Follow every runtime import/resource fact through source, helpers and fixtures."""
    index = ModuleIndex(root)
    incomplete: dict[str, tuple[Unknown, ...]] = {}
    edges = _fixture_edges(root, tests, index, incomplete)
    for file in _python_files(root):
        rel = file.relative_to(root).as_posix()
        tree = ast.parse(file.read_text(encoding="utf-8"), filename=rel)
        evidence = collect(tree, rel, index, tops=_TOPS)
        dependencies = edges.setdefault(rel, set())
        for fact in evidence.records:
            if fact.kind is FactKind.RESOURCE:
                dependencies.add(fact.target)
            elif fact.kind in _MODULE_FACTS:
                dependencies.update(module_files(fact.target, index))
            else:
                raise ValueError(f"Unsupported runtime dependency fact kind: {fact.kind}")
        dependencies.update(
            file for module in plugin_modules(tree) for file in module_files(module, index)
        )
        modeled = _MODELED_FIXTURE_READERS.get(rel)
        incomplete[rel] = tuple(item for item in evidence.unknown if item.kind is not modeled)

    reverse = _tests_by_input(edges, tests)
    unknown = {item for dependency in reverse for item in incomplete.get(dependency, ())}
    return Impact(
        reverse, tuple(sorted(unknown, key=lambda item: (item.path, item.line, item.expression)))
    )


def _tests_by_input(edges: dict[str, set[str]], tests: frozenset[str]) -> dict[str, set[str]]:
    """Propagate test consumers together, including cycles and shared dependencies."""
    ordered = sorted(tests)
    # Each bit is one test; unions avoid revisiting shared edges for each consumer.
    reached = {test: 1 << offset for offset, test in enumerate(ordered)}
    pending = deque(ordered)
    queued = set(ordered)
    while pending:
        source = pending.popleft()
        queued.remove(source)
        consumer_bits = reached[source]
        for dependency in edges.get(source, ()):
            before = reached.get(dependency, 0)
            after = before | consumer_bits
            if before != after:
                reached[dependency] = after
                if dependency not in queued:
                    pending.append(dependency)
                    queued.add(dependency)
    reverse: dict[str, set[str]] = {}
    for path, bits in reached.items():
        consumer_names: set[str] = set()
        remaining = bits
        while remaining:
            flag = remaining & -remaining
            consumer_names.add(ordered[flag.bit_length() - 1])
            remaining ^= flag
        reverse[path] = consumer_names
    return reverse


@contextmanager
def base_checkout(root: Path, ref: str) -> Generator[Path, None, None]:
    """Read committed base facts with the same file resolver used for the head tree."""
    commit = subprocess.run(  # noqa: S603 - argv-only revision lookup; options explicitly terminated
        ["git", "-C", str(root), "rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    archive = subprocess.run(  # noqa: S603 - commit is a verified Git object identity
        ["git", "-C", str(root), "archive", "--format=tar", commit],
        check=True,
        capture_output=True,
    )
    tracked = subprocess.run(  # noqa: S603 - verified commit, no shell or user-supplied options
        ["git", "-C", str(root), "ls-tree", "-r", "--name-only", "-z", commit],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split("\0")
    with tempfile.TemporaryDirectory(prefix="ava-test-impact-base-") as directory:
        base = Path(directory)
        with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as files:
            files.extractall(base, filter="data")
        omitted = [path for path in tracked if path and not os.path.lexists(base / path)]
        if omitted:
            raise RuntimeError(f"Base archive omitted tracked inputs: {omitted}")
        yield base


def unknown_diagnostics(impact: Impact, *, tree: str) -> tuple[str, ...]:
    """Stable, visible reasons why the runtime graph cannot certify a subset."""
    return tuple(
        f"{tree}:{item.path}:{item.line}: {item.reason}: {item.expression}"
        for item in impact.unknown
    )
