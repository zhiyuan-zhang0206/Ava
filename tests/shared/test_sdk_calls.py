"""Tests for extract_sdk_calls — AST-based SDK call extraction."""

from shared.sdk_call_extract import SdkCall, extract_sdk_calls


def _methods(calls: list[SdkCall]) -> list[tuple[str, int]]:
    return [(c.method, c.count) for c in calls]


class TestExtractSdkCalls:
    def test_basic_call(self):
        assert _methods(extract_sdk_calls('ava.files.read("foo")')) == [("files.read", 1)]

    def test_multiple_calls_grouped(self):
        code = 'ava.files.read("a")\nava.files.write("b", 1)\nava.files.read("c")'
        assert _methods(extract_sdk_calls(code)) == [
            ("files.read", 2),
            ("files.write", 1),
        ]

    def test_ties_break_by_method_name(self):
        assert _methods(extract_sdk_calls("ava.shell.run()\nava.agents.spawn()")) == [
            ("agents.spawn", 1),
            ("shell.run", 1),
        ]

    def test_string_literal_not_matched(self):
        """The core fix: ava.*() inside a string literal is NOT an SDK call."""
        assert (
            extract_sdk_calls('x = "\u8bf7\u8c03\u7528 ava.agents.spawn() \u6765\u5b8c\u6210"')
            == []
        )

    def test_comment_not_matched(self):
        code = '# \u8fd9\u91cc\u4f7f\u7528 ava.agents.spawn()\nava.shell.run("ls")'
        assert _methods(extract_sdk_calls(code)) == [("shell.run", 1)]

    def test_triple_quoted_docstring_not_matched(self):
        code = '"""docstring with ava.agents.spawn()"""\nava.files.read("x")'
        assert _methods(extract_sdk_calls(code)) == [("files.read", 1)]

    def test_attribute_read_not_matched(self):
        """ava.self.AGENT_ID (no parens) is not a call."""
        assert _methods(extract_sdk_calls("x = ava.self.AGENT_ID\nava.self.log('hi')")) == [
            ("self.log", 1)
        ]

    def test_direct_help_call(self):
        assert _methods(extract_sdk_calls("ava.help(ava)")) == [("help", 1)]

    def test_non_ava_calls_ignored(self):
        code = 'print("hello")\nos.path.join("a", "b")'
        assert extract_sdk_calls(code) == []

    def test_non_ava_identifier_ending_in_ava(self):
        assert extract_sdk_calls("guava.fs.read()\nlava.x()") == []

    def test_syntax_error_returns_empty(self):
        """Streaming partial code (unclosed paren) — graceful empty."""
        assert extract_sdk_calls('ava.files.read("foo"') == []

    def test_deep_namespace(self):
        assert _methods(extract_sdk_calls('ava.mcps.chrome.navigate("url")')) == [
            ("mcps.chrome.navigate", 1)
        ]

    def test_whitespace_before_paren(self):
        assert _methods(extract_sdk_calls("ava.fs.read ('a')")) == [("fs.read", 1)]

    def test_empty_string(self):
        assert extract_sdk_calls("") == []

    def test_whitespace_only(self):
        assert extract_sdk_calls("   \n  ") == []


class TestExtractSdkCallsFromImport:
    """Import styles beyond the bare ``ava.`` literal resolve to the same
    method paths: ``from ava import ...``, aliases, and deep imports."""

    def test_from_import_module_attribute(self):
        assert _methods(extract_sdk_calls('from ava import shell\nshell.run("ls")')) == [
            ("shell.run", 1)
        ]

    def test_from_import_alias(self):
        assert _methods(
            extract_sdk_calls('from ava import shell as sh\nsh.run_background("x")')
        ) == [("shell.run_background", 1)]

    def test_from_import_deep_function(self):
        assert _methods(extract_sdk_calls('from ava.shell import run\nrun("ls")')) == [
            ("shell.run", 1)
        ]

    def test_from_import_deep_function_alias(self):
        assert _methods(extract_sdk_calls('from ava.shell import run as r\nr("ls")')) == [
            ("shell.run", 1)
        ]

    def test_from_import_multiple_names(self):
        code = 'from ava import shell, files\nshell.run("ls")\nfiles.read("a")'
        assert _methods(extract_sdk_calls(code)) == [("files.read", 1), ("shell.run", 1)]

    def test_import_submodule_alias(self):
        assert _methods(extract_sdk_calls('import ava.shell as sh\nsh.run("ls")')) == [
            ("shell.run", 1)
        ]

    def test_import_ava_alias(self):
        assert _methods(extract_sdk_calls('import ava as a\na.files.read("x")')) == [
            ("files.read", 1)
        ]

    def test_from_import_deep_namespace(self):
        assert _methods(extract_sdk_calls('from ava import mcps\nmcps.chrome.navigate("u")')) == [
            ("mcps.chrome.navigate", 1)
        ]

    def test_from_import_counts_per_call(self):
        code = 'from ava import shell\nshell.run("a")\nshell.run("b")\nshell.run_background("c")'
        assert _methods(extract_sdk_calls(code)) == [
            ("shell.run", 2),
            ("shell.run_background", 1),
        ]

    def test_from_import_inside_function(self):
        code = 'def f():\n    from ava import shell\n    shell.run("ls")'
        assert _methods(extract_sdk_calls(code)) == [("shell.run", 1)]

    def test_import_visible_in_nested_function(self):
        code = 'from ava import shell\ndef f():\n    shell.run("ls")'
        assert _methods(extract_sdk_calls(code)) == [("shell.run", 1)]

    def test_method_body_sees_module_import_not_class_shadow(self):
        """LEGB: a method body skips its class body's shadowing assignment, so
        it resolves to the module-level import."""
        code = (
            "from ava import shell\n"
            "class C:\n"
            "    shell = Other()\n"
            "    shell.run('no')\n"
            "    def m(self):\n"
            "        shell.run('yes')\n"
        )
        assert _methods(extract_sdk_calls(code)) == [("shell.run", 1)]


class TestExtractSdkCallsShadowing:
    """A local binding of an imported name never counts as an SDK call."""

    def test_assignment_shadows_from_import(self):
        code = "from ava import shell\nshell = MyShell()\nshell.run('ls')"
        assert extract_sdk_calls(code) == []

    def test_parameter_shadows_from_import(self):
        code = "from ava import shell\ndef f(shell):\n    shell.run('ls')"
        assert extract_sdk_calls(code) == []

    def test_lambda_parameter_shadows_from_import(self):
        code = "from ava import shell\nf = lambda shell: shell.run('ls')"
        assert extract_sdk_calls(code) == []

    def test_for_target_shadows_from_import(self):
        code = "from ava import shell\nfor shell in shells:\n    shell.run('ls')"
        assert extract_sdk_calls(code) == []

    def test_with_as_shadows_from_import(self):
        code = "from ava import shell\nwith ctx() as shell:\n    shell.run('ls')"
        assert extract_sdk_calls(code) == []

    def test_except_as_shadows_from_import(self):
        code = "from ava import shell\ntry:\n    x()\nexcept Error as shell:\n    shell.run('ls')"
        assert extract_sdk_calls(code) == []

    def test_comprehension_target_shadows_from_import(self):
        code = "from ava import shell\n[shell.run('x') for shell in shells]"
        assert extract_sdk_calls(code) == []

    def test_ava_name_shadowed(self):
        assert extract_sdk_calls("ava = something\nava.files.read('x')") == []

    def test_shadowing_is_source_ordered(self):
        """Calls before the reassignment count; calls after it do not."""
        code = "from ava import shell\nshell.run('a')\nshell = other\nshell.run('b')"
        assert _methods(extract_sdk_calls(code)) == [("shell.run", 1)]

    def test_function_local_import_does_not_leak(self):
        """An import inside one function is invisible at module level."""
        code = 'def f():\n    from ava import shell\n    shell.run("x")\nshell.run("y")'
        assert _methods(extract_sdk_calls(code)) == [("shell.run", 1)]
