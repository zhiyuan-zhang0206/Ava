# `ava.help()` renderer unit tests — Python stub format with a leading
# Markdown heading naming the target.
#
# The renderer prints `# <fqn>` followed by Python source-form output:
# modules render as docstring + children entries (functions as
# `def name(sig):` with indented docstring, submodules as
# `from . import name` + orphan docstring, PEP 224 constants as
# `name: type` + orphan docstring). The leading heading is the only
# Markdown; child bodies stay pure stub.

from __future__ import annotations

import contextlib
import inspect
import io
import sys
import textwrap
import types

import ava
from ava import (
    _classify_dir_entry,
    _Constant,
    _format_docstring,
    _format_signature,
    _module_attribute_annotations,
    _module_attribute_docs,
    _module_children,
)


def _fake_module(source: str, name: str = "fakemod") -> types.ModuleType:
    """Build a module whose source is `source` and whose globals reflect
    executing it. `inspect.linecache` is primed so `inspect.getsource(mod)`
    returns `source` verbatim — required by the AST extractors."""
    src = textwrap.dedent(source)
    mod = types.ModuleType(name)
    mod.__file__ = f"<fake:{name}>"
    inspect.linecache.cache[mod.__file__] = (  # type: ignore[attr-defined]
        len(src),
        None,
        src.splitlines(keepends=True),
        mod.__file__,
    )
    sys.modules[name] = mod
    exec(src, mod.__dict__)
    return mod


def _render(target: object | list[object]) -> str:
    targets = target if isinstance(target, list) else [target]
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        ava.help(*targets)
    return buf.getvalue()


# ── _format_docstring ───────────────────────────────────────────────────────


def test_format_docstring_single_line_one_line_form() -> None:
    # Single-phrase doc renders as one-line triple-quoted literal.
    out = _format_docstring("Single phrase.", indent="")
    assert out == '"""Single phrase."""'


def test_format_docstring_multi_line_first_line_inline() -> None:
    # First line of multi-line doc sits on opening triple; body follows.
    doc = "Summary.\n\nDetail body."
    out = _format_docstring(doc, indent="")
    assert out == '"""Summary.\n\nDetail body.\n"""'


def test_format_docstring_respects_indent() -> None:
    # Indent prefix applies to every line including closing triple.
    doc = "Summary.\n\nBody line."
    out = _format_docstring(doc, indent="    ")
    expected = '    """Summary.\n\n    Body line.\n    """'
    assert out == expected


def test_format_docstring_falls_back_to_single_quote_triple_when_double_present() -> None:
    # If the docstring itself contains `"""`, fall back to `'''` triple-quote.
    doc = 'Use `"""triple"""` for literals.'
    out = _format_docstring(doc, indent="")
    assert out == "'''Use `\"\"\"triple\"\"\"` for literals.'''"


# ── module target = docstring + children as source-form ─────────────────────


def test_module_target_renders_heading_then_docstring() -> None:
    mod = _fake_module(
        '''
        """Module summary."""

        def hello() -> str:
            """Return greeting."""
            return "hi"
        ''',
        name="t_mod_docstring",
    )
    out = _render(mod)
    # Heading line first, then a blank line, then the docstring.
    assert out.startswith('# t_mod_docstring\n\n"""Module summary."""')


def test_module_target_function_child_is_def_with_indented_docstring() -> None:
    mod = _fake_module(
        '''
        """Module summary."""

        def hello() -> str:
            """Return greeting."""
            return "hi"
        ''',
        name="t_func_child",
    )
    out = _render(mod)
    assert "def hello() -> str:" in out
    assert '    """Return greeting."""' in out


def test_module_target_function_without_docstring_uses_ellipsis() -> None:
    mod = _fake_module(
        '''
        """Module summary."""

        def bare(): pass
        ''',
        name="t_func_bare",
    )
    out = _render(mod)
    assert "def bare(): ..." in out


def test_module_target_heading_only_at_top() -> None:
    """One leading `# fqn` heading; child bodies stay pure stub (no extra `##`)."""
    mod = _fake_module(
        '''
        """Module summary."""

        def f1() -> None:
            """one"""

        def f2() -> None:
            """two"""
        ''',
        name="t_no_md",
    )
    out = _render(mod)
    lines = out.splitlines()
    # Exactly one heading, at the top.
    heading_lines = [line for line in lines if line.startswith("#") and not line.startswith("#!")]
    assert heading_lines == ["# t_no_md"], heading_lines


def test_heading_depth_auto_from_fqn_dots() -> None:
    """Depth = FQN dot count + 1. `ava` → `#`, `ava.shell` → `##`,
    `ava.shell.run` → `###`."""
    out = _render(ava)
    assert out.startswith("# ava\n\n"), out.splitlines()[:1]

    out = _render(ava.shell)
    assert out.startswith("## ava.shell\n\n"), out.splitlines()[:1]

    out = _render(ava.shell.run)
    assert out.startswith("### ava.shell.run\n\n"), out.splitlines()[:1]


# ── function target = bare def stub ─────────────────────────────────────────


def test_function_target_renders_heading_then_def_with_signature() -> None:
    # `help(fn)` → `### ava.X.y` heading (depth = dot count + 1) + `def
    # name(sig):` + indented docstring. The `def` line uses the FQN's terminal
    # segment, not the (possibly wrapped) `__name__` attribute.
    out = _render(ava.shell.run)
    assert out.startswith("### ava.shell.run\n\n"), out.splitlines()[0]
    assert "def run(cmd: str" in out
    assert "Non-zero exit does not" in out  # name restatement trimmed (sysprompt verbosity audit)


# ── submodule child = `from . import` + orphan docstring ────────────────────


def test_submodule_child_rendered_as_from_dot_import() -> None:
    """When a module's `__all_for_ava__` includes a submodule, child renders as
    `from . import name` + the submodule's docstring as an orphan literal."""
    out = _render(ava.shell)
    assert "from . import sessions" in out
    # The sessions submodule docstring should appear right after as an orphan literal.
    assert "Persistent shell sessions" in out


# ── class child = expanded surface (fields + methods) ───────────────────────


def test_class_child_expands_dataclass_fields() -> None:
    """A dataclass child renders `class X:` + docstring + each field as
    `name: type` (declaration order), so the agent discovers the attributes
    without instantiating — e.g. ava.web.FetchResult's text/title/url."""
    mod = _fake_module(
        '''
        """Module."""

        from dataclasses import dataclass

        __all_for_ava__ = ["Result"]

        @dataclass
        class Result:
            """A fetched thing."""
            title: str
            url: str
            truncated: bool
        ''',
        name="t_class_fields",
    )
    out = _render(mod)
    assert "class Result:" in out
    assert '"""A fetched thing."""' in out
    # fields rendered as `name: type`, indented under the class
    assert "    title: str" in out
    assert "    url: str" in out
    assert "    truncated: bool" in out
    # declaration order preserved (title before url before truncated)
    assert out.index("title: str") < out.index("url: str") < out.index("truncated: bool")


def test_class_child_expands_methods_with_signature() -> None:
    """A class child's public methods expand to `def name(sig): doc`, just like
    a free function does — not hidden behind a drill-in."""
    mod = _fake_module(
        '''
        """Module."""

        __all_for_ava__ = ["Widget"]

        class Widget:
            """A widget."""
            def render(self, scale: int) -> str:
                """Draw it."""
                return ""
        ''',
        name="t_class_methods",
    )
    out = _render(mod)
    assert "class Widget:" in out
    assert "    def render(self, scale: int) -> str:" in out
    assert "Draw it." in out


def test_class_child_compact_keeps_fields_drops_methods_and_nested() -> None:
    """In compact mode (system prompt) a class child keeps its field
    annotations — so the agent still sees the attribute names — but drops
    methods and nested classes to save tokens."""
    mod = _fake_module(
        '''
        """Module."""

        from dataclasses import dataclass

        __all_for_ava__ = ["Widget"]

        @dataclass
        class Widget:
            """A widget."""
            title: str
            scale: int

            def render(self) -> str:
                """Draw it."""
                return ""

            class Nested:
                """A nested class."""
                x: int
        ''',
        name="t_class_compact",
    )
    token = ava._COMPACT_CLASSES.set(True)
    try:
        out = _render(mod)
    finally:
        ava._COMPACT_CLASSES.reset(token)
    assert "class Widget:" in out
    assert '"""A widget."""' in out
    # fields kept
    assert "    title: str" in out
    assert "    scale: int" in out
    # method + nested class dropped
    assert "def render" not in out
    assert "Draw it." not in out
    assert "class Nested" not in out


def test_class_child_compact_keeps_enum_members() -> None:
    """Enum members are the legal-value contract — compact mode still shows
    them (the enum branch is unaffected by the compact flag)."""
    mod = _fake_module(
        '''
        """Module."""

        from enum import StrEnum

        __all_for_ava__ = ["Status"]

        class Status(StrEnum):
            ON = "on"
            OFF = "off"
        ''',
        name="t_enum_compact",
    )
    token = ava._COMPACT_CLASSES.set(True)
    try:
        out = _render(mod)
    finally:
        ava._COMPACT_CLASSES.reset(token)
    assert "class Status(StrEnum):" in out
    assert "ON = 'on'" in out
    assert "OFF = 'off'" in out


# ── PEP 224 constant child ──────────────────────────────────────────────────


def test_pep224_constant_child_renders_name_type_with_docstring() -> None:
    mod = _fake_module(
        '''
        """Module summary."""

        AGENT_ID: int
        """Your agent id."""
        ''',
        name="t_pep224",
    )
    out = _render(mod)
    assert "AGENT_ID: int" in out
    assert '"""Your agent id."""' in out


# ── ava.const() documented constant ─────────────────────────────────────────


def test_documented_const_short_value_renders_inline_assignment() -> None:
    """A scalar `ava.const()` value renders as a one-line `name: T = value`."""
    c = ava.const(7, doc="The answer-ish.")
    out = ava._format_documented_const_stub("LIMIT", c)
    assert out == 'LIMIT: int = 7\n"""The answer-ish."""'


def test_documented_const_multiline_string_renders_triple_quoted_block() -> None:
    """A multi-line string const (e.g. a skill body) renders as a triple-quoted
    block with the doc as a lead-in — not a stiff `name: str = <... one line>`."""
    c = ava.const("line one\nline two\nline three", doc="A text block.")
    out = ava._format_documented_const_stub("BODY", c)
    assert out == '"""A text block."""\nBODY: str = """\nline one\nline two\nline three\n"""'


# ── multi-target ────────────────────────────────────────────────────────────


def test_multi_target_separates_with_blank_line() -> None:
    """`help(a, b)` renders a, then blank line, then b."""
    out = _render([ava.shell.run, ava.files.read])
    blocks = out.split("\n\n")
    assert any("def run(" in b for b in blocks)
    assert any("def read(" in b for b in blocks)


# ── _format_signature: stringified annotation cleanup ───────────────────────


def test_format_signature_strips_stringified_annotation_quotes() -> None:
    """`from __future__ import annotations` makes hints `'str | None'`; the
    renderer strips outer quotes so agent sees `str | None`."""

    def fn(x: str | None = None) -> None: ...

    # Force annotations to string form (simulating __future__ annotations).
    fn.__annotations__ = {"x": "str | None", "return": "None"}
    sig = _format_signature(fn)
    assert sig == "(x: str | None = None) -> None"


def test_format_signature_strips_typing_dot_prefix() -> None:
    """`typing.Any` → `Any` for cleaner reads."""
    import typing

    def fn(x: typing.Any) -> typing.Any: ...

    sig = _format_signature(fn)
    assert "typing." not in sig


# ── _module_children / _classify_dir_entry ──────────────────────────────────


def test_module_children_uses_all_whitelist_when_present() -> None:
    mod = _fake_module(
        '''
        """Module."""

        __all_for_ava__ = ["public"]

        def public() -> None: """yes"""
        def _internal() -> None: """no"""
        def alsohidden() -> None: """no"""
        ''',
        name="t_all_whitelist",
    )
    children = dict(_module_children(mod))
    assert "public" in children
    assert "_internal" not in children
    assert "alsohidden" not in children  # not in __all_for_ava__


def test_module_children_drops_underscore_names_in_surface() -> None:
    """The underscore guard is applied to `__all_for_ava__` itself, not only to
    the dir() fallback: a private helper a plugin appended to the surface list
    (`register_namespace_member` never allows it, but a hand-built list could)
    must not leak into `help()`. This is the one guard, written once in
    `agent_visible_names`."""
    mod = _fake_module(
        '''
        """Module."""

        __all_for_ava__ = ["public", "_leaked"]

        def public() -> None: """yes"""
        def _leaked() -> None: """must not render despite being in the surface list"""
        ''',
        name="t_surface_underscore_guard",
    )
    children = dict(_module_children(mod))
    assert "public" in children
    assert "_leaked" not in children


def test_module_children_skips_underscore_when_no_all() -> None:
    mod = _fake_module(
        '''
        """Module without __all_for_ava__."""

        def public_one() -> None: """yes"""
        def public_two() -> None: """yes"""
        def _internal() -> None: """no"""
        ''',
        name="t_no_all",
    )
    children = dict(_module_children(mod))
    assert "public_one" in children
    assert "public_two" in children
    assert "_internal" not in children


def test_classify_dir_entry_returns_constant_for_module_attr_string() -> None:
    """Module-level int/str/etc. (not callable/class/module) → wrapped as _Constant."""
    mod = _fake_module(
        '''
        VERSION: str = "1.0"
        """Library version."""
        ''',
        name="t_const_classify",
    )
    annotations = _module_attribute_annotations(mod)
    docs = _module_attribute_docs(mod)
    entry = _classify_dir_entry(mod, "VERSION", annotations, docs)
    assert isinstance(entry, _Constant)
    assert entry.type_str == "str"
    assert entry.doc == "Library version."


def test_documented_const_path_under_home_renders_tilde_relative() -> None:
    """The snapshot test pins ava.memory.PATH to an already-~-relative value,
    so it cannot catch a regression here — this locks the rewrite itself."""
    from pathlib import Path

    c = ava.const(Path.home() / ".ava" / "memory", doc="x")
    out = ava._format_documented_const_stub("PATH", c)
    assert "PATH: PosixPath = ~/.ava/memory" in out


def test_class_stub_renders_enum_members_and_bases() -> None:
    """Enum members are instances — the generic annotation/function walk drops
    them — yet they ARE the legal-value contract; bases carry the hierarchy."""
    mod = _fake_module(
        '''
        """Module."""

        from enum import StrEnum

        __all_for_ava__ = ["Status"]

        class Status(StrEnum):
            ON = "on"
            OFF = "off"
        '''
    )
    out = _render(mod)
    assert "class Status(StrEnum):" in out
    assert "ON = 'on'" in out
    assert "OFF = 'off'" in out


def test_class_stub_suppresses_dataclass_auto_doc_and_mro_doc() -> None:
    """A doc-less dataclass auto-synthesizes `Name(field: type)` as __doc__
    (signature noise), and a doc-less subclass would inherit its base's doc
    via inspect.getdoc's MRO walk — neither may render."""
    mod = _fake_module(
        '''
        """Module."""

        from dataclasses import dataclass

        __all_for_ava__ = ["Child", "Row"]

        @dataclass
        class Row:
            id: int

        class Base(Exception):
            """Base doc that must not leak into Child."""

        class Child(Base): ...
        '''
    )
    out = _render(mod)
    assert "Row(id" not in out  # auto-synthesized signature doc suppressed
    assert (
        out.count("Base doc that must not leak") == 0
    )  # Base not in __all_for_ava__; Child must not inherit it
    assert "class Child(Base): ..." in out
