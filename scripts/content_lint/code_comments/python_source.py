import ast
import inspect
import io
import re
import tokenize
from collections import Counter
from collections.abc import Collection
from dataclasses import dataclass, replace
from itertools import pairwise

from scripts.content_lint.code_comments.directives import (
    ambient_call_lines,
    comment_kind,
    comment_text,
    link_target,
    note_header,
)
from scripts.content_lint.code_comments.fixtures import fixture_owners
from scripts.content_lint.code_comments.model import Note, NoteKind, Source, Symbol


def assignment_name(node: ast.stmt) -> str | None:
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        return node.target.id
    if (
        isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
    ):
        return node.targets[0].id
    return None


def attribute_docs(body: list[ast.stmt]) -> list[tuple[str, ast.Expr]]:
    result: list[tuple[str, ast.Expr]] = []
    for previous, node in pairwise(body):
        name = assignment_name(previous)
        if (
            name
            and isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            result.append((name, node))
    return result


def indentation(line: str) -> int:
    return len(line[: len(line) - len(line.lstrip())].expandtabs(8))


def body_tail_end(
    node: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef, lines: list[str]
) -> int:
    end = node.end_lineno or node.lineno
    first = node.body[0]
    if lines[first.lineno - 1][: first.col_offset].strip():
        return end
    body_indent = indentation(lines[first.lineno - 1])
    for index in range(end, len(lines)):
        line = lines[index]
        if not line.strip():
            continue
        if not line.lstrip().startswith("#") or indentation(line) < body_indent:
            break
        end = index + 1
    return end


class SymbolCollector(ast.NodeVisitor):
    def __init__(self, text: str, fixtures: frozenset[str]) -> None:
        self.symbols = {"<module>": Symbol("<module>", 1, max(1, len(text.splitlines())))}
        self.definitions = list(self.symbols.values())
        self.notes: list[Note] = []
        self.scope: list[str] = []
        self.docstrings: set[int] = set()
        self.fixtures = fixtures
        self.text = text
        self.lines = text.splitlines()

    def collect_body(
        self, node: ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef
    ) -> None:
        name = ".".join(self.scope) or "<module>"
        value = ast.get_docstring(node, clean=True)
        if value is not None:
            first = node.body[0]
            self.docstrings.add(id(first))
            self.notes.append(
                Note(
                    NoteKind.FIXTURE_HELP if name in self.fixtures else NoteKind.DOCSTRING,
                    first.lineno,
                    first.end_lineno or first.lineno,
                    value,
                    name,
                    raw_text=ast.get_source_segment(self.text, first) or "",
                )
            )
        for attribute, expression in attribute_docs(node.body):
            self.docstrings.add(id(expression))
            owner = ".".join([*self.scope, attribute])
            text = ast.literal_eval(expression.value)
            self.notes.append(
                Note(
                    NoteKind.DOCSTRING,
                    expression.lineno,
                    expression.end_lineno or expression.lineno,
                    inspect.cleandoc(text),
                    owner,
                    raw_text=ast.get_source_segment(self.text, expression) or "",
                )
            )
        self.generic_visit(node)

    def visit_Module(self, node: ast.Module) -> None:
        self.collect_body(node)

    def collect_definition(
        self, node: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef
    ) -> None:
        self.scope.append(node.name)
        name = ".".join(self.scope)
        parameters: tuple[str, ...] = ()
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            args = node.args
            parameters = tuple(arg.arg for arg in [*args.posonlyargs, *args.args, *args.kwonlyargs])
        start = min([node.lineno, *(item.lineno for item in node.decorator_list)])
        self.symbols[name] = Symbol(
            name,
            start,
            node.end_lineno or node.lineno,
            parameters,
            name in self.symbols,
            isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef),
        )
        self.definitions.append(
            replace(self.symbols[name], end_line=body_tail_end(node, self.lines))
        )
        self.collect_body(node)
        self.scope.pop()

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.collect_definition(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.collect_definition(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self.collect_definition(node)

    def visit_Expr(self, node: ast.Expr) -> None:
        if (
            isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
            and id(node) not in self.docstrings
        ):
            owner = ".".join(self.scope) or "<module>"
            self.notes.append(
                Note(
                    NoteKind.UNUSED_STRING,
                    node.lineno,
                    node.end_lineno or node.lineno,
                    node.value.value,
                    owner,
                    raw_text=ast.get_source_segment(self.text, node) or "",
                )
            )
        self.generic_visit(node)

    def collect_assignment(self, node: ast.Assign | ast.AnnAssign) -> None:
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        for target in targets:
            if isinstance(target, ast.Name):
                name = ".".join([*self.scope, target.id])
                self.symbols.setdefault(
                    name, Symbol(name, node.lineno, node.end_lineno or node.lineno)
                )
        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        self.collect_assignment(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self.collect_assignment(node)


def enclosing_definition(symbols: Collection[Symbol], line: int, *, following: bool) -> Symbol:
    enclosing = [
        item for item in symbols if item.name != "<module>" and item.line <= line <= item.end_line
    ]
    if enclosing:
        return min(enclosing, key=lambda item: item.end_line - item.line)
    adjacent = [item for item in symbols if item.line == line + 1] if following else []
    if len(adjacent) == 1:
        return adjacent[0]
    return next(item for item in symbols if item.name == "<module>")


def comment_owner(symbols: Collection[Symbol], line: int, *, following: bool = True) -> str:
    return enclosing_definition(symbols, line, following=following).name


def definition_identities(definitions: list[Symbol]) -> list[Symbol]:
    parents: list[int] = [0]
    groups: list[tuple[int, str, str]] = [(0, "module", "<module>")]
    for index, definition in enumerate(definitions[1:], 1):
        parent = enclosing_definition(definitions[:index], definition.line, following=False)
        parent_index = definitions.index(parent)
        parents.append(parent_index)
        groups.append(
            (parent_index, "function" if definition.callable else "class", definition.name)
        )
    totals = Counter(groups)
    ordinal: Counter[tuple[int, str, str]] = Counter()
    result = [replace(definitions[0], lexical_key="<module>")]
    for index, definition in enumerate(definitions[1:], 1):
        group = groups[index]
        ordinal[group] += 1
        key = f"{result[parents[index]].lexical_key}/{group[1]}:{definition.name}[{ordinal[group]}/{totals[group]}]"
        result.append(replace(definition, lexical_key=key))
    return result


@dataclass(frozen=True)
class Block:
    line: int
    end_line: int
    kind: NoteKind
    text: str
    links: tuple[str, ...]
    raw_text: str


def comment_blocks(
    comments: list[tuple[int, int, str]], ambient: frozenset[int]
) -> list[Block]:
    blocks: list[Block] = []
    index = 0
    while index < len(comments):
        line, column, text = comments[index]
        if note_header(text):
            body: list[str] = []
            links: list[str] = []
            raw = [text]
            last = line
            cursor = index + 1
            while cursor < len(comments):
                next_line, next_column, next_text = comments[cursor]
                if (
                    next_column != column
                    or next_line != last + 1
                    or note_header(next_text)
                    or link_target(next_text) is not None
                    or comment_kind(next_text, next_line, ambient) != NoteKind.COMMENT
                ):
                    break
                body.append(comment_text(next_text))
                raw.append(next_text)
                last = next_line
                cursor += 1
            while cursor < len(comments):
                next_line, next_column, next_text = comments[cursor]
                target = link_target(next_text)
                if next_column != column or next_line != last + 1 or target is None:
                    break
                links.append(target)
                raw.append(next_text)
                last = next_line
                cursor += 1
            blocks.append(
                Block(
                    line,
                    last,
                    NoteKind.NOTE,
                    "\n".join(body).strip("\n"),
                    tuple(links),
                    "\n".join(raw),
                )
            )
            index = cursor
            continue
        target = link_target(text)
        if target is not None:
            links = [target]
            raw = [text]
            last = line
            cursor = index + 1
            while cursor < len(comments):
                next_line, next_column, next_text = comments[cursor]
                found = link_target(next_text)
                if next_column != column or next_line != last + 1 or found is None:
                    break
                links.append(found)
                raw.append(next_text)
                last = next_line
                cursor += 1
            blocks.append(Block(line, last, NoteKind.NOTE, "", tuple(links), "\n".join(raw)))
            index = cursor
            continue
        blocks.append(
            Block(
                line,
                line,
                comment_kind(text, line, ambient),
                comment_text(text),
                (),
                text,
            )
        )
        index += 1
    return blocks


def parse_python(path: str, text: str) -> Source:
    tree = ast.parse(text, filename=path)
    collector = SymbolCollector(text, fixture_owners(tree))
    collector.visit(tree)
    collector.definitions = definition_identities(collector.definitions)
    collector.notes = [
        replace(
            note,
            lexical_owner=enclosing_definition(
                collector.definitions, note.line, following=False
            ).lexical_key,
        )
        for note in collector.notes
    ]
    ambient = ambient_call_lines(path, tree)
    comments: list[tuple[int, int, str]] = []
    for item in tokenize.generate_tokens(io.StringIO(text).readline):
        if item.type != tokenize.COMMENT:
            continue
        if item.start == (1, 0) and re.fullmatch(
            r"#!/usr/bin/(?:env python(?:3(?:\.12)?)?|python3(?:\.12)?)", item.string
        ):
            continue
        comments.append((item.start[0], item.start[1], item.string))
    for block in comment_blocks(comments, ambient):
        machine = block.kind in {NoteKind.LINT_DIRECTIVE, NoteKind.MACHINE}
        anchor = block.end_line
        lexical_owner = comment_owner(collector.definitions, anchor, following=not machine)
        owner = (
            lexical_owner
            if machine
            else comment_owner(collector.symbols.values(), anchor, following=True)
        )
        scope = enclosing_definition(
            collector.definitions, anchor, following=not machine
        ).lexical_key
        collector.notes.append(
            Note(
                block.kind,
                block.line,
                block.end_line,
                block.text,
                owner,
                scope,
                block.raw_text,
                block.links,
            )
        )
    return Source(
        path,
        collector.symbols,
        tuple(sorted(collector.notes, key=lambda item: (item.line, item.kind))),
    )
