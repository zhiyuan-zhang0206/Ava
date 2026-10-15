from __future__ import annotations

import ast
import textwrap
from pathlib import Path

import pytest

from scripts.lint.agent_docstrings import (
    SKILL_REF_RE,
    discover_agent_surface_modules,
    docstring_violations,
    is_in_scope,
    module_doc_child_reference_violations,
    top_level_surface,
)


def _parse(source: str) -> tuple[ast.Module, list[str]]:
    src = textwrap.dedent(source)
    return ast.parse(src), src.splitlines()


def _module_violations(source: str) -> list[str]:
    tree, lines = _parse(source)
    return [reason for _, reason in module_doc_child_reference_violations(tree, lines)]


def test_child_name_in_code_span_is_flagged() -> None:
    out = _module_violations(
        '''
        """Watch things. Use `launch(code, timeout)` for a custom condition."""

        def launch(code, timeout): ...
        '''
    )
    assert out and "restates child `launch`" in out[0]


def test_child_name_in_plain_prose_passes() -> None:
    out = _module_violations(
        '''
        """Long-term notes, with semantic search to find them."""

        def search(query): ...
        '''
    )
    assert out == []


def test_cross_module_reference_passes() -> None:
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
    tree, lines = _parse(
        '''
        """Watch things."""

        def launch(code): ...

        def cron(expr):
            """A prebuilt watcher; see `launch`."""
        '''
    )
    assert module_doc_child_reference_violations(tree, lines) == []


def _function_violations(source: str, *, with_skill_rule: bool) -> list[str]:
    tree, lines = _parse(source)
    extra = [(SKILL_REF_RE, "SDK<->skill coupling")] if with_skill_rule else []
    out: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef):
            out.extend(r for _, r in docstring_violations(node, lines, extra=extra))
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
    out = _function_violations(
        '''
        def names():
            """List every skill's metadata."""
        ''',
        with_skill_rule=False,
    )
    assert out == []


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
    out = _function_violations(
        '''
        def call(**kwargs):
            """Forward **kwargs to the tool; extra **args are rejected."""
        ''',
        with_skill_rule=False,
    )
    assert out == []


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


def _write(path: Path, source: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(source))


def test_discover_agent_surface_modules(tmp_path: Path) -> None:
    ava_dir = tmp_path / "ava"

    _write(ava_dir / "__init__.py", '__all_for_ava__ = ["files", "shell"]\n')
    _write(ava_dir / "files.py", '"""Files stub."""\n')
    _write(ava_dir / "shell" / "__init__.py", '"""Shell namespace."""\n')
    _write(
        ava_dir / "shell" / "sessions.py",
        '''
        __all_for_ava__ = ["new"]


        def new():
            """Start a session."""
        ''',
    )
    _write(ava_dir / "framework_core.py", '"""Framework module with a public name."""\n')
    _write(ava_dir / "_internal.py", '"""Private framework module."""\n')
    _write(ava_dir / "_x.py", "__all_for_ava__: list[str] = []\n")
    _write(
        ava_dir / "mcps_like.py",
        """
        class P:
            @property
            def __all_for_ava__(self):
                ...
        """,
    )

    surface = discover_agent_surface_modules(tmp_path)

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
        top_level_surface(tmp_path)


def test_is_in_scope_consults_provided_surface_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    surface = {(tmp_path / "ava" / "files.py").resolve()}

    assert is_in_scope(Path("ava/files.py"), set(), surface) is True
    assert is_in_scope(Path("ava/framework_core.py"), set(), surface) is False


def test_is_in_scope_plugin_paths_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    namespace_helper = (tmp_path / "ava_builtins/plugins/x/_walk.py").resolve()
    plugin_namespace_files = {namespace_helper}

    assert (
        is_in_scope(Path("ava_builtins/plugins/x/plugin.py"), plugin_namespace_files, set()) is True
    )
    assert (
        is_in_scope(Path("ava_builtins/plugins/x/_walk.py"), plugin_namespace_files, set()) is True
    )
    assert (
        is_in_scope(Path("ava_builtins/plugins/x/other_helper.py"), plugin_namespace_files, set())
        is False
    )


def test_real_repo_surface_excludes_underscore_and_includes_init_modules() -> None:
    repo_root = Path(__file__).resolve().parents[3]

    surface = discover_agent_surface_modules(repo_root)

    assert (repo_root / "ava/agents/__init__.py").resolve() in surface
    assert (repo_root / "ava/shell/__init__.py").resolve() in surface
    assert (repo_root / "ava/sdk_surface/agent_identity.py").resolve() not in surface
    assert (repo_root / "ava/sdk_surface/wraps.py").resolve() not in surface
    assert (repo_root / "base/packages/plugins/loading/plugin_loader.py").resolve() not in surface
