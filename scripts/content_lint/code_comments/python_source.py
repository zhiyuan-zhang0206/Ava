import ast
import inspect
import io
import re
import tokenize
from itertools import pairwise

from scripts.content_lint.code_comments.directives import (
    ambient_call_lines,
    comment_kind,
    is_quiesce_reference,
)
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


class SymbolCollector(ast.NodeVisitor):
    def __init__(self, text: str) -> None:
        self.symbols = {"<module>": Symbol("<module>", 1, max(1, len(text.splitlines())))}
        self.definitions = dict(self.symbols)
        self.notes: list[Note] = []
        self.scope: list[str] = []
        self.docstrings: set[int] = set()

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
                    NoteKind.DOCSTRING, first.lineno, first.end_lineno or first.lineno, value, name
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
        self.definitions[name] = self.symbols[name]
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


def comment_owner(symbols: dict[str, Symbol], line: int) -> str:
    enclosing = [
        item
        for item in symbols.values()
        if item.name != "<module>" and item.line <= line <= item.end_line
    ]
    if enclosing:
        return min(enclosing, key=lambda item: item.end_line - item.line).name
    following = [item for item in symbols.values() if item.line == line + 1]
    if len(following) == 1:
        return following[0].name
    return "<module>"


# ava:ref scripts/content_lint/code_comments/docs/code_comments.ava.okf.md#python-entry-shebang
def parse_python(path: str, text: str) -> Source:
    tree = ast.parse(text, filename=path)
    collector = SymbolCollector(text)
    collector.visit(tree)
    ambient_lines = ambient_call_lines(path, tree)
    for item in tokenize.generate_tokens(io.StringIO(text).readline):
        if item.type != tokenize.COMMENT:
            continue
        if item.start == (1, 0) and re.fullmatch(
            r"#!/usr/bin/(?:env python(?:3(?:\.12)?)?|python3(?:\.12)?)", item.string
        ):
            continue
        kind = comment_kind(item.string, item.start[0], ambient_lines)
        lexical_owner = comment_owner(collector.definitions, item.start[0])
        owner = (
            lexical_owner
            if kind == NoteKind.LINT_DIRECTIVE or is_quiesce_reference(item.string)
            else comment_owner(collector.symbols, item.start[0])
        )
        collector.notes.append(
            Note(kind, item.start[0], item.end[0], item.string, owner, lexical_owner)
        )
    return Source(
        path,
        collector.symbols,
        tuple(sorted(collector.notes, key=lambda item: (item.line, item.kind))),
    )
