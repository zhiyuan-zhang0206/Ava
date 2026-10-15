import ast
import inspect
from collections import defaultdict
from dataclasses import dataclass

from scripts.content_lint.code_comments.check import Repository, check_contract, load_contracts
from scripts.content_lint.code_comments.model import Kind
from scripts.content_lint.code_comments.python_source import assignment_name, attribute_docs

type Definition = (
    ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef | ast.Assign | ast.AnnAssign
)


@dataclass(frozen=True)
class Projection:
    node: Definition
    existing: ast.Expr | None


def projection(node: Definition, body: list[ast.stmt]) -> Projection:
    if isinstance(node, ast.Assign | ast.AnnAssign):
        docs = dict(attribute_docs(body))
        return Projection(node, docs.get(assignment_name(node) or ""))
    first = node.body[0] if node.body else None
    existing = (
        first
        if isinstance(first, ast.Expr)
        and isinstance(first.value, ast.Constant)
        and isinstance(first.value.value, str)
        else None
    )
    return Projection(node, existing)


def definition(tree: ast.Module, name: str) -> Projection:
    node: Definition = tree
    parent_body = tree.body
    if name == "<module>":
        return projection(tree, tree.body)
    for part in name.split("."):
        if isinstance(node, ast.Assign | ast.AnnAssign):
            raise TypeError(f"cannot traverse an assignment: {name}")
        parent_body = node.body
        candidates = [
            child
            for child in parent_body
            if (
                isinstance(child, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
                and child.name == part
            )
            or assignment_name(child) == part
        ]
        if len(candidates) != 1:
            raise ValueError(f"cannot generate an ambiguous or conditional definition: {name}")
        candidate = candidates[0]
        if not isinstance(
            candidate,
            ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef | ast.Assign | ast.AnnAssign,
        ):
            raise TypeError(f"unsupported definition: {name}")
        node = candidate
    return projection(node, parent_body)


class WithoutDocstrings(ast.NodeTransformer):
    def visit(self, node: ast.AST) -> ast.AST:
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            attributes = {id(expression) for _, expression in attribute_docs(node.body)}
            if ast.get_docstring(node, clean=False) is not None:
                node.body = node.body[1:]
            node.body = [child for child in node.body if id(child) not in attributes]
        return super().visit(node)


def assignment_edit(
    node: ast.Assign | ast.AnnAssign, literal: bytes, lines: list[bytes], offsets: list[int]
) -> tuple[int, int, bytes]:
    end = node.end_lineno or node.lineno
    last = lines[end - 1]
    if last[(node.end_col_offset or 0) :].strip():
        raise ValueError("put API assignment on its own line before generating")
    start = offsets[end]
    indent = lines[node.lineno - 1][: node.col_offset]
    prefix = b"" if last.endswith(b"\n") else b"\n"
    return start, start, prefix + indent + literal + b"\n"


def projection_edit(
    target: Projection, text: str, lines: list[bytes], offsets: list[int]
) -> tuple[int, int, bytes] | None:
    node, first = target.node, target.existing
    if first is not None and inspect.cleandoc(ast.literal_eval(first.value)) == text:
        return None
    literal = repr(text).encode("utf-8")
    if first is not None:
        start = offsets[first.lineno - 1] + first.col_offset
        end = offsets[(first.end_lineno or first.lineno) - 1] + (first.end_col_offset or 0)
        return start, end, literal
    if isinstance(node, ast.Assign | ast.AnnAssign):
        return assignment_edit(node, literal, lines, offsets)
    if isinstance(node, ast.Module):
        start = len(lines[0]) if lines and lines[0].startswith(b"#!") else 0
        return start, start, literal + b"\n"
    first = node.body[0]
    if first.lineno == node.lineno:
        start = offsets[first.lineno - 1] + first.col_offset
        return start, start, literal + b"; "
    start = offsets[first.lineno - 1]
    indent = lines[first.lineno - 1][: first.col_offset]
    return start, start, indent + literal + b"\n"


def render_source(source: str, statements: dict[str, str]) -> str:
    tree = ast.parse(source)
    lines = source.encode("utf-8").splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    edits: list[tuple[int, int, bytes]] = []
    for name, text in statements.items():
        edit = projection_edit(definition(tree, name), text, lines, offsets)
        if edit is not None:
            edits.append(edit)
    output = source.encode("utf-8")
    for start, end, replacement in sorted(edits, reverse=True):
        output = output[:start] + replacement + output[end:]
    rendered = output.decode("utf-8")
    old = WithoutDocstrings().visit(ast.parse(source))
    new = WithoutDocstrings().visit(ast.parse(rendered))
    if ast.dump(old) != ast.dump(new):
        raise ValueError("API generation changed executable syntax")
    return rendered


def render(repo: Repository) -> list[str]:
    contracts, findings = load_contracts(repo)
    if findings:
        raise ValueError("\n".join(item.render() for item in findings))
    by_path: dict[str, dict[str, str]] = defaultdict(dict)
    for contract in contracts.values():
        if contract.kind != Kind.API:
            continue
        check_contract(repo, contract, projection=False)
        path, symbol = contract.code[0].split(":", 1)
        if symbol in by_path[path]:
            raise ValueError(f"multiple API documentation owners for {path}:{symbol}")
        by_path[path][symbol] = contract.statement
    pending = {
        path: render_source(repo.text(path), statements) for path, statements in by_path.items()
    }
    changed = [path for path, text in pending.items() if text != repo.text(path)]
    for path in changed:
        (repo.root / path).write_text(pending[path], encoding="utf-8")
        repo.sources.pop(path, None)
    return sorted(changed)
