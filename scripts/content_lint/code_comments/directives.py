import ast
import re
from pathlib import PurePosixPath

from scripts.content_lint.code_comments.model import NoteKind

_KEY = r"[A-Za-z0-9_./-]+\.meiva\.okf\.md#[a-z][a-z0-9]*(?:-[a-z0-9]+)*"
_REFERENCE = re.compile(rf"(?:#|//) meiva:ref ({_KEY})")
_QUIESCE = re.compile(rf"# quiesce-exempt: meiva:ref ({_KEY})")
_RUFF_CODE = r"[A-Z]+[0-9]{3,4}"
_PYRIGHT_CODE = r"report[A-Z][A-Za-z0-9]+"
_LINT = re.compile(
    rf"# noqa: {_RUFF_CODE}(?:, ?{_RUFF_CODE})*"
    rf"|# pyright: ignore\[{_PYRIGHT_CODE}(?:, ?{_PYRIGHT_CODE})*\]"
)


def reference_key(text: str, *, python: bool) -> str | None:
    match = _REFERENCE.fullmatch(text)
    if match is None and python:
        match = _QUIESCE.fullmatch(text)
    return match[1] if match is not None else None


# meiva:ref scripts/content_lint/code_comments/docs/code_comments.meiva.okf.md#python-machine-directives
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


# meiva:ref scripts/content_lint/code_comments/docs/code_comments.meiva.okf.md#python-machine-directives
def comment_kind(text: str, line: int, ambient_lines: frozenset[int]) -> NoteKind:
    if _LINT.fullmatch(text):
        return NoteKind.LINT_DIRECTIVE
    if line in ambient_lines and re.fullmatch(r"# ambient: [A-Z][A-Z0-9]*", text):
        return NoteKind.MACHINE
    return NoteKind.COMMENT


def is_quiesce_reference(text: str) -> bool:
    return _QUIESCE.fullmatch(text) is not None
