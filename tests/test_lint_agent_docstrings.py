"""Unit tests for the 2026-06-10 prompt-slim additions to
scripts/lint_agent_docstrings.py: module-docstring child-name restating,
SDK<->skill coupling, and the new impl/reverse-reference keywords. The
pre-existing CJK / keyword machinery is exercised implicitly (same code path).
"""

from __future__ import annotations

import ast
import textwrap
from pathlib import Path

import pytest

from scripts.lint_agent_docstrings import (
    _SKILL_REF_RE,
    _discover_agent_surface_modules,
    _docstring_violations,
    _is_in_scope,
    _module_doc_child_reference_violations,
    _top_level_surface,
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


# ── agent-surface whitelist scope (2026-09) ──────────────────────────────────
#
# Which `ava/` files get agent-docstring linting used to be decided by the `_`
# prefix (any underscore path segment => out of scope, which accidentally also
# excluded every `__init__.py`). It's now decided by the agent-surface
# whitelist: a module declares `__all_for_ava__` itself, or it is the
# `ava/<name>.py` / `ava/<name>/__init__.py` for a `<name>` listed in
# `ava/__init__.py`'s `__all_for_ava__`. The underscore plays no part.


def _write(path: Path, source: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(source))


def test_discover_agent_surface_modules(tmp_path: Path) -> None:
    ava_dir = tmp_path / "ava"

    _write(ava_dir / "__init__.py", '__all_for_ava__ = ["files", "shell"]\n')
    # Top-level namespace module (no marker of its own) -> IN.
    _write(ava_dir / "files.py", '"""Files stub."""\n')
    # Namespace package `__init__` (no marker) -> IN.
    _write(ava_dir / "shell" / "__init__.py", '"""Shell namespace."""\n')
    # Declares its own marker -> IN, regardless of not being a namespace.
    _write(
        ava_dir / "shell" / "sessions.py",
        '''
        __all_for_ava__ = ["new"]


        def new():
            """Start a session."""
        ''',
    )
    # The key regression: a framework module with a public name, no marker,
    # and not listed as a namespace -> OUT.
    _write(ava_dir / "framework_core.py", '"""Framework module with a public name."""\n')
    # Private-prefixed, no marker -> OUT (underscore alone proves nothing).
    _write(ava_dir / "_extend.py", '"""Private framework module."""\n')
    # Annotated assignment form of the marker -> IN.
    _write(ava_dir / "_x.py", "__all_for_ava__: list[str] = []\n")
    # Property form of the marker -> IN.
    _write(
        ava_dir / "mcps_like.py",
        """
        class P:
            @property
            def __all_for_ava__(self):
                ...
        """,
    )

    surface = _discover_agent_surface_modules(tmp_path)

    expected = {
        (ava_dir / "__init__.py").resolve(),
        (ava_dir / "files.py").resolve(),
        (ava_dir / "shell" / "__init__.py").resolve(),
        (ava_dir / "shell" / "sessions.py").resolve(),
        (ava_dir / "_x.py").resolve(),
        (ava_dir / "mcps_like.py").resolve(),
    }
    assert surface == expected


def test_top_level_surface_raises_without_marker(tmp_path: Path) -> None:
    _write(tmp_path / "ava" / "__init__.py", '"""No marker declared here."""\n')

    with pytest.raises(ValueError):
        _top_level_surface(tmp_path)


def test_is_in_scope_consults_provided_surface_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    surface = {(tmp_path / "ava" / "files.py").resolve()}

    assert _is_in_scope(Path("ava/files.py"), set(), surface) is True
    assert _is_in_scope(Path("ava/framework_core.py"), set(), surface) is False


def test_is_in_scope_plugin_paths_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    namespace_helper = (tmp_path / "ava_builtins/plugins/x/_walk.py").resolve()
    plugin_namespace_files = {namespace_helper}

    # plugin.py itself is always scanned (dev-facing wrap-target check).
    assert (
        _is_in_scope(Path("ava_builtins/plugins/x/plugin.py"), plugin_namespace_files, set())
        is True
    )
    # A helper bound to a namespace -> in scope.
    assert (
        _is_in_scope(Path("ava_builtins/plugins/x/_walk.py"), plugin_namespace_files, set()) is True
    )
    # A helper NOT bound to any namespace -> out of scope.
    assert (
        _is_in_scope(Path("ava_builtins/plugins/x/other_helper.py"), plugin_namespace_files, set())
        is False
    )


def test_real_repo_surface_excludes_underscore_and_includes_init_modules() -> None:
    # Regression check against the actual repo tree: `ava/agents/__init__.py`
    # and `ava/shell/__init__.py` were previously excluded by the `_` rule
    # (`__init__.py` itself starts with an underscore); `ava/agent_identity.py`
    # and `ava/_extend.py` declare no marker and are not listed as a namespace,
    # so they stay out under the new rule too.
    repo_root = Path(__file__).resolve().parents[1]

    surface = _discover_agent_surface_modules(repo_root)

    assert (repo_root / "ava/agents/__init__.py").resolve() in surface
    assert (repo_root / "ava/shell/__init__.py").resolve() in surface
    assert (repo_root / "ava/agent_identity.py").resolve() not in surface
    assert (repo_root / "ava/_extend.py").resolve() not in surface
