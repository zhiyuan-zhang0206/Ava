from dataclasses import dataclass
from enum import StrEnum


class NoteKind(StrEnum):
    COMMENT = "comment"
    NOTE = "note"
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
class Symbol:
    name: str
    line: int
    end_line: int
    parameters: tuple[str, ...] = ()
    ambiguous: bool = False
    callable: bool = False
    lexical_key: str = ""


@dataclass(frozen=True)
class Note:
    kind: NoteKind
    line: int
    end_line: int
    text: str
    owner: str
    lexical_owner: str = ""
    raw_text: str = ""
    links: tuple[str, ...] = ()


@dataclass(frozen=True)
class Source:
    path: str
    symbols: dict[str, Symbol]
    notes: tuple[Note, ...]
