import hashlib
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from scripts.content_lint.code_comments.check import Repository
from scripts.content_lint.code_comments.model import Note, NoteKind, Source, Symbol

SUFFIXES = (".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs")


class ParserRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ParserSymbol(ParserRecord):
    name: str
    line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    parameters: list[str]
    ambiguous: bool
    callable: bool


class ParserNote(ParserRecord):
    kind: Literal[NoteKind.COMMENT, NoteKind.DOCSTRING, NoteKind.UNUSED_STRING]
    line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    text: str
    owner: str


class ParserSource(ParserRecord):
    path: str
    sha256: str
    symbols: dict[str, ParserSymbol]
    notes: list[ParserNote]


class ParserOutput(ParserRecord):
    version: Literal[1]
    sources: list[ParserSource]


def parse_source(item: ParserSource) -> Source:
    path = item.path
    symbols: dict[str, Symbol] = {}
    for name, value in item.symbols.items():
        if value.name != name or value.end_line < value.line:
            raise ValueError(f"invalid TypeScript symbol: {path}:{name}")
        symbols[name] = Symbol(
            name,
            value.line,
            value.end_line,
            tuple(value.parameters),
            value.ambiguous,
            value.callable,
        )
    if "<module>" not in symbols:
        raise ValueError(f"missing TypeScript module: {path}")
    notes: list[Note] = []
    for value in item.notes:
        if value.owner not in symbols or value.end_line < value.line:
            raise ValueError(f"invalid TypeScript note: {path}:{value.line}")
        notes.append(Note(value.kind, value.line, value.end_line, value.text, value.owner))
    return Source(path, symbols, tuple(notes))


def load_index(repo: Repository, text: str) -> None:
    payload = ParserOutput.model_validate_json(text)
    expected = {path for path in repo.files if path.endswith(SUFFIXES)}
    found: dict[str, Source] = {}
    for item in payload.sources:
        path = item.path
        if path not in expected or path in found:
            raise ValueError(f"unexpected or duplicate TypeScript source: {path}")
        digest = hashlib.sha256(repo.text(path).encode("utf-8")).hexdigest()
        if item.sha256 != digest:
            raise ValueError(f"stale TypeScript parser output: {path}")
        found[path] = parse_source(item)
    if set(found) != expected:
        raise ValueError(f"TypeScript parser omitted: {', '.join(sorted(expected - found.keys()))}")
    repo.sources.update(found)
