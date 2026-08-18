"""Unit tests for the 2026-06-10 prompt-slim additions to
scripts/lint_agent_docstrings.py: module-docstring child-name restating,
SDK<->skill coupling, and the new impl/reverse-reference keywords. The
pre-existing CJK / keyword machinery is exercised implicitly (same code path).
"""

from __future__ import annotations

import ast
import textwrap

from scripts.lint_agent_docstrings import (
    _SKILL_REF_RE,
    _docstring_violations,
    _module_doc_child_reference_violations,
)


def _parse(source: str) -> tuple[ast.Module, list[str]]:
    src = textwrap.dedent(source)
    return ast.parse(src), src.splitlines()


def _module_violations(source: str) -> list[str]:
    tree, lines = _parse(source)
    return [reason for _, reason in _module_doc_child_reference_violations(tree, lines)]


# ── module docstring restating its own children ─────────────────────────────


def test_child_name_in_code_span_is_flagged() -> None:
    out = _module_violations(
        '''
        """Watch things. Use `launch(code, timeout)` for a custom condition."""

        def launch(code, timeout): ...
        '''
    )
    assert out and "restates child `launch`" in out[0]


def test_child_name_in_plain_prose_passes() -> None:
    # The zero-false-positive core: a child's name used as an English word
    # (no backticks) is never flagged — "semantic search" vs `search()`.
    out = _module_violations(
        '''
        """Long-term notes, with semantic search to find them."""

        def search(query): ...
        '''
    )
    assert out == []


def test_cross_module_reference_passes() -> None:
    # `ava.shell.run` mentions another module's function — `run` is not a
    # child of this module, so the span is fine.
    out = _module_violations(
        '''
        """Persistent sessions. For one-shot commands use `ava.shell.run`."""

        def new(): ...
        '''
    )
    assert out == []


def test_private_child_not_counted() -> None:
    out = _module_violations(
        '''
        """Uses `_spawn` internally."""

        def _spawn(): ...
        '''
    )
    assert out == []


def test_function_docstring_may_reference_siblings() -> None:
    # The rule covers the MODULE docstring only — a function explaining its
    # relationship to a sibling (cron -> launch) is legitimate and untouched.
    tree, lines = _parse(
        '''
        """Watch things."""

        def launch(code): ...

        def cron(expr):
            """A prebuilt watcher; see `launch`."""
        '''
    )
    assert _module_doc_child_reference_violations(tree, lines) == []


# ── SDK<->skill coupling keyword ─────────────────────────────────────────────


def _function_violations(source: str, *, with_skill_rule: bool) -> list[str]:
    tree, lines = _parse(source)
    extra = [(_SKILL_REF_RE, "SDK<->skill coupling")] if with_skill_rule else []
    out: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef):
            out.extend(r for _, r in _docstring_violations(node, lines, extra=extra))
    return out


def test_skill_reference_flagged_outside_skills_module() -> None:
    out = _function_violations(
        '''
        def show(name):
            """For boilerplate, invoke the `ui` skill."""
        ''',
        with_skill_rule=True,
    )
    assert any("coupling" in r for r in out)


def test_skill_reference_allowed_when_rule_absent() -> None:
    # _check_file drops the rule for ava/skills.py — modeled here by passing
    # no extra patterns.
    out = _function_violations(
        '''
        def names():
            """List every skill's metadata."""
        ''',
        with_skill_rule=False,
    )
    assert out == []


# ── Markdown emphasis ────────────────────────────────────────────────────────


def test_markdown_bold_flagged() -> None:
    out = _function_violations(
        '''
        def search(query):
            """Search the **shared pool** for notes."""
        ''',
        with_skill_rule=False,
    )
    assert any("Markdown emphasis" in r for r in out)


def test_double_star_kwargs_passes() -> None:
    # Signature-style `**kwargs` has no closing pair — not emphasis.
    out = _function_violations(
        '''
        def call(**kwargs):
            """Forward **kwargs to the tool; extra **args are rejected."""
        ''',
        with_skill_rule=False,
    )
    assert out == []


# ── new keywords ride the existing pipeline ──────────────────────────────────


def test_new_impl_keywords_flagged() -> None:
    out = _function_violations(
        '''
        def get_status(agent_id):
            """Reads the agents_meta row; shown in the frontend popover.
            Returned by the gateway."""
        ''',
        with_skill_rule=False,
    )
    joined = "\n".join(out)
    assert "agents_meta" in joined
    assert "presentation detail" in joined
    assert "reverse reference" in joined
