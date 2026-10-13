"""`meizel.toml`: parsing and structural validation of one directory's local graph."""

from __future__ import annotations

import json
import re
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import cast

from meizel.contracts.debt import (
    CYCLE_EDGE,
    TABLE_KEY,
    DebtTable,
    local_edge,
    owner_hint,
    parse_debt,
)
from meizel.graph.graphs import cycle_through, strongly_connected, unneeded_back_edges
from meizel.graph.model import Code, Diagnostic
from meizel.graph.universe import MANIFEST, Universe
from meizel.roles import check_table

__all__ = ["Manifest", "load_manifests", "manifest_path"]

_TOP_KEYS = ("exports", "uses", "loads", "external", "roles", TABLE_KEY)
_EdgeReader = Callable[[str, tuple[str, ...], int], tuple[str, ...]]


def manifest_path(directory: str) -> str:
    return f"{directory}/{MANIFEST}" if directory else MANIFEST


def first(path: str) -> str:
    """The first component of a relative path: the direct child it lies under."""
    return path.split("/", 1)[0]


def within(path: str, prefix: str) -> bool:
    """`path` is `prefix` or lies below it (a file is a prefix of itself)."""
    return path == prefix or path.startswith(f"{prefix}/")


@dataclass(slots=True)
class Manifest:
    directory: str
    exports: tuple[str, ...] = ()
    uses: dict[str, tuple[str, ...]] = field(default_factory=dict[str, tuple[str, ...]])
    loads: dict[str, tuple[str, ...]] = field(default_factory=dict[str, tuple[str, ...]])
    external: dict[str, str] = field(default_factory=dict[str, str])
    roles: dict[str, tuple[str, ...]] = field(default_factory=dict[str, tuple[str, ...]])
    debt: DebtTable = field(default_factory=dict[Code, dict[str, int]])
    back: frozenset[tuple[str, str]] = frozenset()
    dangling: list[tuple[str, str | None, str | None]] = field(
        default_factory=list[tuple[str, str | None, str | None]]
    )
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

    def debt_line(self, code: Code, key: str) -> int:
        """Line of a recorded debt, or of its table when the key is written differently."""
        header = re.compile(rf"^\s*\[\s*debt\s*\.\s*{re.escape(code.value)}\s*\]")
        start = next((n for n, text in enumerate(self.lines) if header.match(text)), None)
        if start is None:
            return 1
        quoted = json.dumps(key, ensure_ascii=False)
        for number in range(start + 1, len(self.lines)):
            text = self.lines[number].strip()
            if text.startswith("["):
                break
            if text.startswith((quoted, f"'{key}'")):
                return number + 1
        return start + 1

    def graph(self) -> dict[str, tuple[str, ...]]:
        """The declared edges between direct children: every endpoint cut to its first component."""
        projected: dict[str, list[str]] = {}
        for source, targets in self.uses.items():
            known = projected.setdefault(first(source), [])
            known.extend(t for t in dict.fromkeys(first(t) for t in targets) if t not in known)
        return {source: tuple(targets) for source, targets in projected.items()}

    def pairs(self) -> list[tuple[str, str]]:
        """Every declared `(source path, target path)`, each relative to this directory."""
        return [(source, target) for source, targets in self.uses.items() for target in targets]

    def covering(self, src: str, dst: str) -> list[tuple[str, str]]:
        """The declared pairs a fact from `src` to `dst` (relative paths) falls under."""
        return [(s, d) for s, d in self.pairs() if within(src, s) and within(dst, d)]

    def narrowed(self, cf: str, ct: str) -> list[tuple[str, str]]:
        """The declared pairs of the edge `cf -> ct` that name something deeper than a child."""
        return [(s, d) for s, d in self.pairs() if (first(s), first(d)) == (cf, ct)]

    def back_edges(self) -> list[tuple[str, str]]:
        """The declared edges whose facts are recorded as closing a cycle."""
        return sorted(self.back)


class _Reader:
    """Validates one parsed document, collecting diagnostics instead of raising."""

    def __init__(self, universe: Universe, directory: str, lines: list[str]) -> None:
        self.universe = universe
        self.directory = directory
        self.manifest = Manifest(directory, lines=lines)
        self.diagnostics: list[Diagnostic] = []
        self.children = frozenset(universe.children.get(directory, ()))

    def report(self, line: int, code: Code, message: str, hints: tuple[str, ...] = ()) -> None:
        self.diagnostics.append(Diagnostic(self.manifest.path, line, code, message, hints))

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

    def exists(self, relative: str) -> bool:
        """A tracked file or directory below this one, named by a normalised relative path."""
        full = f"{self.directory}/{relative}" if self.directory else relative
        parts = relative.split("/")
        clean = bool(relative) and all(part not in {"", ".", ".."} for part in parts)
        return (
            clean
            and parts[-1] != MANIFEST
            and (full in self.universe.files or full in self.universe.dirs)
        )

    def child(self, name: str, where: str, line: int, *, deep: bool = False) -> bool:
        if name in self.children or (deep and self.exists(name)):
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
        self.manifest.external = self.read_external(document.get("external", {}))
        if "roles" in document:
            self.read_roles(document["roles"])
        if TABLE_KEY in document:
            line = self.manifest.line_of(TABLE_KEY)
            self.manifest.debt = parse_debt(
                document[TABLE_KEY], lambda text: self.report(line, Code.MANIFEST_DEBT, text)
            )
            self.manifest.back = frozenset(
                local_edge(key) for key in self.manifest.debt.get(CYCLE_EDGE, ())
            )
        self.check_cycles()

    def read_roles(self, table: object) -> None:
        """`[roles]`, root only: every top-level directory with Python has exactly one role."""
        line = self.manifest.line_of("roles")
        if self.directory:
            self.report(line, Code.MANIFEST_ROLES, "[roles] belongs in the root manifest only")
            return
        roles, problems = check_table(table)
        for key, message in problems:
            self.report(self.manifest.line_of(key), Code.MANIFEST_ROLES, message)
        self.manifest.roles = roles
        listed = {name for names in roles.values() for name in names}
        for name in sorted(listed):
            if name not in self.universe.dirs:
                self.report(
                    self.manifest.line_of(name),
                    Code.MANIFEST_ROLES,
                    f"role entry {name!r} is not a tracked top-level directory",
                )
        python_tops = {path.split("/", 1)[0] for path in self.universe.python_files if "/" in path}
        for name in sorted(python_tops - listed):
            if not name.startswith("."):
                self.report(
                    line,
                    Code.MANIFEST_ROLES,
                    f"top-level directory {name!r} holds Python but has no role; "
                    "declare it under [roles]",
                )

    def read_external(self, table: object) -> dict[str, str]:
        """`[external]`: files that run code from outside the repository, each with a reason."""
        if not isinstance(table, dict):
            self.report(
                self.manifest.line_of("external"),
                Code.MANIFEST_SYNTAX,
                "[external] must be a table",
            )
            return {}
        result: dict[str, str] = {}
        for key, reason in cast("dict[str, object]", table).items():
            line = self.manifest.line_of(key)
            if not key.endswith(".py"):
                self.report(
                    line, Code.MANIFEST_CHILD, f"[external] key {key!r} must be a Python file"
                )
            elif not self.child(key, "[external]", line):
                self.manifest.dangling.append(("external", key, None))
                continue
            elif not isinstance(reason, str) or not reason.strip():
                self.report(
                    line,
                    Code.MANIFEST_SYNTAX,
                    f"[external] {key!r} needs a non-empty reason string",
                )
            else:
                result[key] = reason.strip()
        return result

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
            else:
                self.manifest.dangling.append(("exports", None, name))
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
            if self.child(key, f"[{section}]", line, deep=section == "uses"):
                values = self.names(value, f"[{section}] {key!r}", line)
                result[key] = reader(key, values, line)
            else:
                self.manifest.dangling.append((section, key, None))
        return result

    def read_use(self, source: str, targets: tuple[str, ...], line: int) -> tuple[str, ...]:
        kept: list[str] = []
        for target in targets:
            if first(target) == first(source):
                self.report(
                    line,
                    Code.MANIFEST_SELF_EDGE,
                    f"{source!r} cannot use {target!r}: both lie under {first(source)!r}",
                )
            elif self.child(target, f"[uses] {source!r}", line, deep=True):
                kept.append(target)
            else:
                self.manifest.dangling.append(("uses", source, target))
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
                self.manifest.dangling.append(("loads", source, target))
        return tuple(kept)

    def check_cycles(self) -> None:
        """The declared graph minus its recorded back-edges must be acyclic."""
        uses = self.manifest.graph()
        back = self.manifest.back_edges()
        carried = set(back)
        base = {
            src: tuple(t for t in targets if (src, t) not in carried)
            for src, targets in uses.items()
        }
        hint = (owner_hint(self.directory, self.manifest.debt),) if self.manifest.debt else ()
        for component in strongly_connected(base):
            cycle = cycle_through(base, component)
            line = self.manifest.line_of(cycle[0])
            self.report(
                line, Code.MANIFEST_CYCLE, "cycle in the local graph: " + " -> ".join(cycle), hint
            )
        for src, dst in unneeded_back_edges(uses, back):
            reason = (
                "closes no cycle any more" if dst in uses.get(src, ()) else "is no longer declared"
            )
            key = next(k for k in self.manifest.debt[CYCLE_EDGE] if local_edge(k) == (src, dst))
            self.report(
                self.manifest.debt_line(CYCLE_EDGE, key),
                Code.DEBT_PAID,
                f"{CYCLE_EDGE} debt for {src} -> {dst} is recorded but the edge {reason}",
                ("run `python -m meizel debt prune` to remove it",),
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
