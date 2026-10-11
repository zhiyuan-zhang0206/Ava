"""Value types shared by every stage of the checker."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

__all__ = ["Code", "Diagnostic", "Fact", "FactKind", "MeizelError", "Problem", "Restricted"]


class MeizelError(Exception):
    """The repository cannot be analysed at all (git failure, unparsable source)."""


class FactKind(StrEnum):
    IMPORT = "import"
    ATTRIBUTE = "attribute"
    FIXTURE = "fixture"
    PLUGIN = "plugin"
    PATCH = "patch"
    LOAD = "load"


class Code(StrEnum):
    UNDECLARED_EDGE = "undeclared-edge"
    NOT_EXPORTED = "not-exported"
    PRIVATE_MODULE = "private-module"
    PRIVATE_NAME = "private-name"
    UNRESOLVED_IMPORT = "unresolved-import"
    RESTRICTED = "restricted-python"
    STALE_LOAD = "stale-load"
    MANIFEST_SYNTAX = "manifest-syntax"
    MANIFEST_KEY = "manifest-unknown-key"
    MANIFEST_CHILD = "manifest-unknown-child"
    MANIFEST_DUPLICATE = "manifest-duplicate"
    MANIFEST_SELF_EDGE = "manifest-self-edge"
    MANIFEST_CYCLE = "manifest-cycle"
    MANIFEST_EXPORT_PRIVATE = "manifest-export-private"
    MANIFEST_STALE_EDGE = "manifest-stale-edge"


@dataclass(frozen=True, slots=True)
class Fact:
    """File `src` uses `dst`: a file, or a directory for a declared dynamic load."""

    src: str
    dst: str
    line: int
    kind: FactKind
    names: tuple[str, ...] = ()
    detail: str = ""

    def edge(self) -> str:
        return f"{self.src} -> {self.dst}"


@dataclass(frozen=True, slots=True)
class Restricted:
    """A form outside the restricted Python subset, found in one file."""

    line: int
    form: str
    detail: str = ""


@dataclass(frozen=True, slots=True)
class Problem:
    """A source-level defect found while extracting facts."""

    line: int
    code: Code
    message: str


@dataclass(frozen=True, slots=True)
class Diagnostic:
    path: str
    line: int
    code: Code
    message: str
    hints: tuple[str, ...] = ()

    def render(self) -> str:
        head = f"{self.path}:{self.line}: [{self.code}] {self.message}"
        return "\n".join([head, *(f"    {hint}" for hint in self.hints)])
