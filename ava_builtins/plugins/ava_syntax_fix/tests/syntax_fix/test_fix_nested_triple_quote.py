"""Syntax fix cases: fix nested triple quote."""

from __future__ import annotations

import ast

from ava_builtins.plugins.ava_syntax_fix._deterministic_fixes import (
    apply_all_deterministic_fixes,
    fix_escape_inner_quotes,
    fix_nested_triple_quote,
)
from ava_builtins.plugins.ava_syntax_fix.tests.test_syntax_fix import _is_broken


class TestFixNestedTripleQuote:
    """Code/markdown written as a triple-quoted string whose body contains
    `\"\"\"` docstrings -- the inner triples close the outer string. The fix
    escapes the interior triples, preserving the runtime content exactly."""

    def test_inner_docstring_escaped_and_content_preserved(self):
        code = 'src = """\ndef f():\n    """doc"""\n    return 1\n"""'
        assert _is_broken(code)
        fixed, n = fix_nested_triple_quote(code)
        assert n >= 1
        compile(fixed, "<t>", "exec")
        ns: dict = {}
        exec(fixed, ns)  # pyright: ignore[reportUnknownArgumentType]
        assert '"""doc"""' in ns["src"]  # interior triple survives as text

    def test_single_quote_delim_nested(self):
        code = "src = '''\nclass C:\n    '''doc'''\n    x = 1\n'''"
        assert _is_broken(code)
        fixed, n = fix_nested_triple_quote(code)
        assert n >= 1
        compile(fixed, "<t>", "exec")

    def test_adjacent_edit_args_not_merged(self):
        # files.edit(old=\"\"\"...\"\"\", new=\"\"\"...with \"\"\"doc\"\"\"...\"\"\")
        # Only the docstring inside `new` may be escaped. The real delimiters of
        # `old` and `new` hug `=` / `,` -- escaping them would merge the two
        # kwargs into one wrong-but-compiling string and silently drop `new=`.
        # The boundary guard must keep both kwargs intact.
        code = (
            "ava.files.edit(\n"
            '    "m.py",\n'
            '    old="""def f() -> int: ...""",\n'
            '    new="""def g() -> bool:\n'
            '    """doc for g"""\n'
            '    return True""",\n'
            ")"
        )
        assert _is_broken(code)
        fixed, n = fix_nested_triple_quote(code)
        assert n >= 1
        compile(fixed, "<t>", "exec")
        edit = next(
            node
            for node in ast.walk(ast.parse(fixed))
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "edit"
        )
        kwargs = {kw.arg for kw in edit.keywords}
        assert {"old", "new"} <= kwargs  # both args preserved, not merged

    def test_edit_docstring_in_old_not_merged(self):
        # Variant: docstring is in `old=`, `new=` is clean.
        # The boundary guard must escape only the interior docstring, not the
        # real delimiters hugging `=` or `,`.
        code = (
            "ava.files.edit(\n"
            '    "m.py",\n'
            '    old="""def f() -> int:\n'
            '    """doc for f"""\n'
            '    return 42""",\n'
            '    new="""def g() -> bool: return True""",\n'
            ")"
        )
        assert _is_broken(code)
        fixed, n = fix_nested_triple_quote(code)
        assert n >= 1
        compile(fixed, "<t>", "exec")
        edit = next(
            node
            for node in ast.walk(ast.parse(fixed))
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "edit"
        )
        kwargs = {kw.arg for kw in edit.keywords}
        assert {"old", "new"} <= kwargs  # both args preserved, not merged

    def test_edit_docstring_in_both_not_merged(self):
        # Both `old=` and `new=` contain docstrings.  This is a multi-site
        # corruption: escaping only one interior pair leaves the other still
        # broken, and escaping across the real delimiter boundary would merge
        # the two kwargs.  The fixer correctly refuses (n=0) rather than
        # produce a wrong-but-compiling result -- this falls through to LLM
        # repair, which is the right call.
        code = (
            "ava.files.edit(\n"
            '    "m.py",\n'
            '    old="""def f() -> int:\n'
            '    """doc for f"""\n'
            '    return 42""",\n'
            '    new="""def g() -> bool:\n'
            '    """doc for g"""\n'
            '    return True""",\n'
            ")"
        )
        assert _is_broken(code)
        fixed, n = fix_nested_triple_quote(code)
        # Honest refusal: the fixer cannot fix both interior docstrings
        # without merging the real delimiters, so it defers to LLM repair.
        assert n == 0
        assert fixed == code

    def test_write_adjacent_call_not_merged(self):
        # files.write() adjacent to files.edit() -- the boundary guard must
        # not merge the two separate call expressions.
        code = (
            "ava.files.write(\n"
            '    "m.py",\n'
            '    """def helper() -> None:\n'
            '    """nested doc"""\n'
            '    pass"""\n'
            ")\n"
            "ava.files.edit(\n"
            '    "m.py",\n'
            '    old="""x = 1""",\n'
            '    new="""x = 2""",\n'
            ")\n"
        )
        assert _is_broken(code)
        fixed, n = fix_nested_triple_quote(code)
        assert n >= 1
        compile(fixed, "<t>", "exec")
        tree = ast.parse(fixed)
        calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        ]
        # Both write and edit calls survive as separate calls
        assert len(calls) >= 2
        attrs = {node.func.attr for node in calls if isinstance(node.func, ast.Attribute)}
        assert {"write", "edit"} <= attrs

    def test_boundary_only_nesting_refused(self):
        # When the ONLY way to compile is to escape an operator-adjacent triple
        # (a real delimiter of a separate literal), the fixer refuses and leaves
        # the error for the repair step rather than merge two literals.
        code = 'a = """x"""\nb = """y"""\nc = """z"""\nd = (1\n'  # broken: open paren
        assert _is_broken(code)
        assert fix_nested_triple_quote(code) == (code, 0)

    def test_valid_triple_quote_no_change(self):
        code = 'x = """hello\nworld"""'
        assert fix_nested_triple_quote(code) == (code, 0)


class TestFixEscapeInnerQuotes:
    """Single-line string with unescaped same-char quotes nested inside
    (`run("grep -n "pat" file")`). Escape the interior quotes -- but refuse
    when an interior quote abuts a `+`/`,`/`(` operator (a real string
    boundary), which would silently merge separate literals."""

    def test_nested_double_quote_escaped(self):
        code = 'print(run("grep -n "pat" file"))'
        assert _is_broken(code)
        fixed, n = fix_escape_inner_quotes(code)
        assert n == 2
        compile(fixed, "<t>", "exec")

    def test_concatenation_boundary_refused(self):
        # The double-quotes are operator-adjacent string boundaries; escaping
        # them would merge three literals into one wrong-but-compiling string,
        # so the fixer must refuse and leave the error for the repair step.
        code = 'print("a" + "b" + "c"\n'  # unterminated paren -> broken
        assert _is_broken(code)
        fixed, n = fix_escape_inner_quotes(code)
        assert n == 0
        assert fixed == code

    def test_valid_no_change(self):
        code = 'print("a" + "b")'
        assert fix_escape_inner_quotes(code) == (code, 0)


class TestApplyAllDeterministicFixes:
    """The combined pass is compile-guarded at the boundary (valid code is
    returned untouched) and per fixer (only a fixer whose output compiles is
    adopted, and the first such fix wins -- so a correct repair is never
    clobbered by a later, blunter fixer)."""

    def test_valid_code_untouched(self):
        # Guard 1: code that already compiles is never modified, even when it
        # contains triple-quotes / nested quotes a fixer could otherwise touch.
        for code in (
            "x = 1 + 2",
            'x = """multi\nline"""',
            'cmd = "grep \\"x\\" f"',
            's = "a" + "b" + "c"',
            "r = run('echo \"hi\"')",
        ):
            fixed, applied = apply_all_deterministic_fixes(code)
            assert fixed == code
            assert applied == []

    def test_single_fixer_repairs_and_compiles(self):
        # Em-dash in code position: unicode_punct alone makes it compile.
        fixed, applied = apply_all_deterministic_fixes("x = 1 \u2014 2")
        assert "\u2014" not in fixed
        compile(fixed, "<t>", "exec")
        assert len(applied) == 1

    def test_unterminated_multiline_repaired(self):
        code = 'r = run("git commit -m \'fix\nbody\nmore")'
        fixed, applied = apply_all_deterministic_fixes(code)
        compile(fixed, "<t>", "exec")
        assert applied and applied[0].startswith("unterminated_to_triple")

    def test_unfixable_returns_original_empty(self):
        # No deterministic fixer can repair this; it must be returned unchanged
        # (left for the LLM repair step), never a partial broken edit.
        code = "def f(:\n    pass"
        fixed, applied = apply_all_deterministic_fixes(code)
        assert fixed == code
        assert applied == []

    def test_applied_label_format(self):
        _fixed, applied = apply_all_deterministic_fixes("x = 1 \u2014 2")
        assert len(applied) == 1
        assert "(" in applied[0] and applied[0].endswith(")")
