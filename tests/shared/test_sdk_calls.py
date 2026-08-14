"""Tests for extract_sdk_calls — AST-based SDK call extraction."""

from shared.timeline import SdkCall, extract_sdk_calls


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
        assert extract_sdk_calls('x = "请调用 ava.agents.spawn() 来完成"') == []

    def test_comment_not_matched(self):
        code = '# 这里使用 ava.agents.spawn()\nava.shell.run("ls")'
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
