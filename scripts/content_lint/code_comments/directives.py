import ast
import re
from pathlib import PurePosixPath

from scripts.content_lint.code_comments.model import NoteKind

_LINK = re.compile(r"(?:#|//)\s*meiva:link\s+(\S+)")
_NOTE = re.compile(r"(?:#|//)\s*meiva:note\s*")
_QUIESCE = re.compile(r"# quiesce-exempt: \S.*")
_RUFF_CODE = r"[A-Z]+[0-9]{3,4}"
_PYRIGHT_CODE = r"report[A-Z][A-Za-z0-9]+"
_LINT = re.compile(
    rf"# noqa: {_RUFF_CODE}(?:, ?{_RUFF_CODE})*"
    rf"|# pyright: ignore\[{_PYRIGHT_CODE}(?:, ?{_PYRIGHT_CODE})*\]"
)


def note_header(text: str) -> bool:
    return _NOTE.fullmatch(text) is not None


def link_target(text: str) -> str | None:
    match = _LINK.fullmatch(text)
    return match[1] if match is not None else None


def comment_text(text: str) -> str:
    body = text[1:]
    return body[1:] if body.startswith(" ") else body


def ambient_call_lines(path: str, tree: ast.Module) -> frozenset[int]:
    if (
        "/tests/" in path
        or path.startswith("tests/")
        or PurePosixPath(path).name.startswith("test_")
        or path == "base/cluster/machine/ambient.py"
    ):
        return frozenset()
    return frozenset(
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and (
            (isinstance(node.func, ast.Name) and node.func.id == "gateway_access_from_settings")
            or (
                isinstance(node.func, ast.Attribute)
                and node.func.attr == "gateway_access_from_settings"
            )
        )
    )


def comment_kind(text: str, line: int, ambient_lines: frozenset[int]) -> NoteKind:
    if _QUIESCE.fullmatch(text):
        return NoteKind.MACHINE
    if _LINT.fullmatch(text):
        return NoteKind.LINT_DIRECTIVE
    if line in ambient_lines and re.fullmatch(r"# ambient: [A-Z][A-Z0-9]*", text):
        return NoteKind.MACHINE
    return NoteKind.COMMENT
