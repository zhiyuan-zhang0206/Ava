from dataclasses import dataclass
from enum import StrEnum


class Kind(StrEnum):
    INVARIANT = "invariant"
    EXTERNAL = "external"
    DECISION = "decision"
    API = "api"


class NoteKind(StrEnum):
    COMMENT = "comment"
    DOCSTRING = "docstring"
    UNUSED_STRING = "unused-string"
    LINT_DIRECTIVE = "lint-directive"
    MACHINE = "machine"
    FIXTURE_HELP = "fixture-help"


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    code: str
    message: str

    def render(self) -> str:
        return f"{self.path}:{self.line}: [{self.code}] {self.message}"


@dataclass(frozen=True)
class Contract:
    document: str
    line: int
    identifier: str
    kind: Kind
    statement: str
    code: tuple[str, ...]
    tests: tuple[str, ...]
    source: str = ""
    applies_to: str = ""
    decision: str = ""
    api: str = ""

    @property
    def key(self) -> str:
        return f"{self.document}#{self.identifier}"

    @property
    def owner(self) -> str:
        return "/".join(self.document.split("/")[: self.document.split("/").index("docs")])


@dataclass(frozen=True)
class Symbol:
    name: str
    line: int
    end_line: int
    parameters: tuple[str, ...] = ()
    ambiguous: bool = False
    callable: bool = False


@dataclass(frozen=True)
class Note:
    kind: NoteKind
    line: int
    end_line: int
    text: str
    owner: str
    lexical_owner: str = ""


@dataclass(frozen=True)
class Source:
    path: str
    symbols: dict[str, Symbol]
    notes: tuple[Note, ...]
