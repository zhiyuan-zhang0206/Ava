"""`meizel.toml`: parsing and structural validation of one directory's local graph."""

from __future__ import annotations

import re
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import cast

from scripts.meizel.graphs import cycle_through, strongly_connected
from scripts.meizel.model import Code, Diagnostic
from scripts.meizel.universe import MANIFEST, Universe

__all__ = ["Manifest", "load_manifests", "manifest_path"]

_TOP_KEYS = ("exports", "uses", "loads")
_EdgeReader = Callable[[str, tuple[str, ...], int], tuple[str, ...]]


def manifest_path(directory: str) -> str:
    return f"{directory}/{MANIFEST}" if directory else MANIFEST


@dataclass(slots=True)
class Manifest:
    directory: str
    exports: tuple[str, ...] = ()
    uses: dict[str, tuple[str, ...]] = field(default_factory=dict[str, tuple[str, ...]])
    loads: dict[str, tuple[str, ...]] = field(default_factory=dict[str, tuple[str, ...]])
    lines: list[str] = field(default_factory=list[str])

    @property
    def path(self) -> str:
        return manifest_path(self.directory)

    def line_of(self, key: str) -> int:
        """Line of `key` in the file, or 1 when it cannot be located."""
        pattern = re.compile(rf"""^\s*["']?{re.escape(key)}["']?\s*=""")
        for number, text in enumerate(self.lines, 1):
            if pattern.match(text):
                return number
        return 1

    def graph(self) -> dict[str, tuple[str, ...]]:
        return dict(self.uses)


class _Reader:
    """Validates one parsed document, collecting diagnostics instead of raising."""

    def __init__(self, universe: Universe, directory: str, lines: list[str]) -> None:
        self.universe = universe
        self.directory = directory
        self.manifest = Manifest(directory, lines=lines)
        self.diagnostics: list[Diagnostic] = []
        self.children = frozenset(universe.children.get(directory, ()))

    def report(self, line: int, code: Code, message: str) -> None:
        self.diagnostics.append(Diagnostic(self.manifest.path, line, code, message))

    def names(self, value: object, where: str, line: int) -> tuple[str, ...]:
        items = cast("list[object]", value) if isinstance(value, list) else None
        if items is None or not all(isinstance(v, str) for v in items):
            self.report(line, Code.MANIFEST_SYNTAX, f"{where} must be a list of strings")
            return ()
        value = cast("list[str]", items)
        seen: set[str] = set()
        for name in value:
            if name in seen:
                self.report(line, Code.MANIFEST_DUPLICATE, f"{where} lists {name!r} twice")
            seen.add(name)
        return tuple(dict.fromkeys(value))

    def child(self, name: str, where: str, line: int) -> bool:
        if name in self.children:
            return True
        self.report(
            line,
            Code.MANIFEST_CHILD,
            f"{where}: {name!r} is not a tracked child of {self.directory or '.'}",
        )
        return False

    def read(self, document: dict[str, object]) -> None:
        for key in document:
            if key not in _TOP_KEYS:
                self.report(self.manifest.line_of(key), Code.MANIFEST_KEY, f"unknown key {key!r}")
        self.read_exports(document.get("exports", []))
        self.manifest.uses = self.read_table(document.get("uses", {}), "uses", self.read_use)
        self.manifest.loads = self.read_table(document.get("loads", {}), "loads", self.read_load)
        self.check_cycles()

    def read_exports(self, value: object) -> None:
        line = self.manifest.line_of("exports")
        kept: list[str] = []
        for name in self.names(value, "exports", line):
            if name.startswith("_"):
                self.report(
                    line,
                    Code.MANIFEST_EXPORT_PRIVATE,
                    f"{name!r} starts with an underscore and cannot be exported",
                )
            elif self.child(name, "exports", line):
                kept.append(name)
        self.manifest.exports = tuple(kept)

    def read_table(
        self, table: object, section: str, reader: _EdgeReader
    ) -> dict[str, tuple[str, ...]]:
        if not isinstance(table, dict):
            self.report(
                self.manifest.line_of(section), Code.MANIFEST_SYNTAX, f"[{section}] must be a table"
            )
            return {}
        result: dict[str, tuple[str, ...]] = {}
        for key, value in cast("dict[str, object]", table).items():
            line = self.manifest.line_of(key)
            if self.child(key, f"[{section}]", line):
                values = self.names(value, f"[{section}] {key!r}", line)
                result[key] = reader(key, values, line)
        return result

    def read_use(self, source: str, targets: tuple[str, ...], line: int) -> tuple[str, ...]:
        kept: list[str] = []
        for target in targets:
            if target == source:
                self.report(line, Code.MANIFEST_SELF_EDGE, f"{source!r} cannot use itself")
            elif self.child(target, f"[uses] {source!r}", line):
                kept.append(target)
        return tuple(kept)

    def read_load(self, source: str, targets: tuple[str, ...], line: int) -> tuple[str, ...]:
        if not source.endswith(".py"):
            self.report(line, Code.MANIFEST_CHILD, f"[loads] key {source!r} must be a Python file")
        kept: list[str] = []
        for target in targets:
            if target in self.universe.dirs:
                kept.append(target)
            else:
                self.report(
                    line,
                    Code.MANIFEST_CHILD,
                    f"[loads] {source!r}: {target!r} is not a tracked directory",
                )
        return tuple(kept)

    def check_cycles(self) -> None:
        graph = self.manifest.graph()
        for component in strongly_connected(graph):
            cycle = cycle_through(graph, component)
            line = self.manifest.line_of(cycle[0])
            self.report(
                line, Code.MANIFEST_CYCLE, "cycle in the local graph: " + " -> ".join(cycle)
            )


def load_manifests(universe: Universe) -> tuple[dict[str, Manifest], list[Diagnostic]]:
    manifests: dict[str, Manifest] = {}
    diagnostics: list[Diagnostic] = []
    for directory in sorted(universe.manifest_dirs):
        path = manifest_path(directory)
        text = (universe.root / path).read_text(encoding="utf-8")
        reader = _Reader(universe, directory, text.splitlines())
        try:
            document = tomllib.loads(text)
        except tomllib.TOMLDecodeError as error:
            reader.report(1, Code.MANIFEST_SYNTAX, f"invalid TOML: {error}")
            diagnostics.extend(reader.diagnostics)
            continue
        reader.read(document)
        manifests[directory] = reader.manifest
        diagnostics.extend(reader.diagnostics)
    return manifests, diagnostics
