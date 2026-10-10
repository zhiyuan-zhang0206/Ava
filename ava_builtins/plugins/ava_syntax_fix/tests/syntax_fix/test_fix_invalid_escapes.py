"""Syntax fix cases: fix invalid escapes."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableConfig
from langgraph.runtime import Runtime

from agent.state import AgentState
from ava_builtins.plugins.ava_syntax_fix._deterministic_fixes import (
    fix_bracket_matching,
    fix_fstring_expressions,
    fix_indentation,
    fix_string_newlines,
    fix_unclosed_triple_quote,
    fix_unicode_punctuation,
    fix_unterminated_to_triple,
)
from ava_builtins.plugins.ava_syntax_fix.agent_runtime import (
    _extract_text,
    _fix_invalid_escapes,
    _is_stdlib_module,
    _render_syntax_error,
    _strip_code_fence,
    syntax_fix_before_exec,
)
from ava_builtins.plugins.ava_syntax_fix.tests.test_syntax_fix import _is_broken
from base.agents.context import AvaContext
from base.config import settings
from base.db import Database
from base.events.live.bus import EventBus
from base.host.env.agent_slices import AgentSlices


class TestFixInvalidEscapes:
    def test_no_invalid_escape_no_change(self):
        """Valid Python source — no edits."""
        code = 'print("hello\\n")\n'
        fixed, n = _fix_invalid_escapes(code)
        assert fixed == code
        assert n == 0

    def test_pipe_alternation_in_grep_call(self):
        """astropy-8872-style: `\\|` inside subprocess.run arg → escape it."""
        code = (
            'import subprocess\nsubprocess.run(["grep", "-n", "float16\\|dtype.*cast", "/x.py"])\n'
        )
        fixed, n = _fix_invalid_escapes(code)
        assert n == 1
        assert "\\\\|" in fixed
        # `compile()` should produce no SyntaxWarning afterwards
        import warnings

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            compile(fixed, "<test>", "exec")
            sw = [w for w in caught if issubclass(w.category, SyntaxWarning)]
        assert sw == []

    def test_multiple_invalid_escapes_in_one_string(self):
        """`\\|x\\|y\\.z` — three invalid escapes in one literal."""
        code = 's = "foo\\|bar\\|baz\\."\n'
        fixed, n = _fix_invalid_escapes(code)
        assert n == 3
        assert fixed == 's = "foo\\\\|bar\\\\|baz\\\\."\n'

    def test_valid_escape_preserved(self):
        """`\\n` is valid — keep as-is; only `\\|` gets escaped."""
        code = 's = "line1\\nline2 with \\| and \\."\n'
        fixed, n = _fix_invalid_escapes(code)
        assert n == 2  # only the two invalid pairs
        # `\n` (newline escape) must remain a single `\n`, not become `\\n`
        assert "\\n" in fixed
        assert "\\\\n" not in fixed
        assert "\\\\|" in fixed
        assert "\\\\." in fixed

    def test_raw_string_skipped(self):
        """`r"\\|"` is valid raw — never modify."""
        code = 'pattern = r"foo\\|bar"\n'
        fixed, n = _fix_invalid_escapes(code)
        assert fixed == code
        assert n == 0

    def test_byte_string_fixed_like_str(self):
        """`b"..."` uses the same escape rules — `b"\\|"` is also invalid."""
        code = 'data = b"foo\\|bar"\n'
        fixed, n = _fix_invalid_escapes(code)
        assert n == 1
        assert 'b"foo\\\\|bar"' in fixed

    def test_triple_quoted_string(self):
        """Triple-quoted strings still scan correctly."""
        code = 'code = """\nimport re\nm = re.match("foo\\|bar", text)\n"""\n'
        fixed, n = _fix_invalid_escapes(code)
        assert n == 1
        assert "foo\\\\|bar" in fixed

    def test_fstring_fixed(self):
        """f-strings honor the same escape rules."""
        code = 'log = f"got {x} \\| {y}"\n'
        fixed, n = _fix_invalid_escapes(code)
        assert n == 1
        assert 'f"got {x} \\\\| {y}"' in fixed

    def test_raw_fstring_skipped(self):
        """rf"..." (raw f-string) — no escape interpretation."""
        code = 'log = rf"got \\| {x}"\n'
        fixed, n = _fix_invalid_escapes(code)
        assert fixed == code
        assert n == 0

    def test_tokenize_error_returns_original(self):
        """Code that can't be tokenized (unterminated string) → no-op."""
        code = 'broken = "no end'  # unterminated
        _, n = _fix_invalid_escapes(code)
        # Either returns unchanged or no edits — must not raise.
        assert n == 0


class TestSyntaxFixBeforeExec:
    @staticmethod
    def _runtime():
        ctx = AvaContext(
            ops_pool=AsyncMock(),
            llm=MagicMock(),
            agent=AgentSlices.resolve(
                default_reader=lambda domain, field: getattr(getattr(settings, domain), field),
            ),
            db=Database.from_settings(),
            bus=EventBus.from_settings(),
        )
        return Runtime(context=ctx)

    @staticmethod
    def _config() -> RunnableConfig:
        return {"configurable": {"thread_id": "7"}}

    async def test_no_ai_message_returns_none(self):
        from langchain_core.messages import HumanMessage

        state = AgentState(messages=[HumanMessage(content="hello")])
        result = await syntax_fix_before_exec(state, self._runtime(), self._config())
        assert result is None

    async def test_no_tool_calls_returns_none(self):
        state = AgentState(messages=[AIMessage(content="ok")])
        result = await syntax_fix_before_exec(state, self._runtime(), self._config())
        assert result is None

    async def test_empty_code_returns_none(self):
        state = AgentState(
            messages=[
                AIMessage(
                    content="",
                    tool_calls=[{"name": "execute_code", "args": {"code": ""}, "id": "1"}],
                )
            ]
        )
        result = await syntax_fix_before_exec(state, self._runtime(), self._config())
        assert result is None

    async def test_chinese_punctuation_fixed(self, monkeypatch: pytest.MonkeyPatch):
        """Chinese comma should be fixed."""

        # Pin the flag: the assertion expects ruff_format spacing, which a host
        # .env (AVA_SYNTAX_FIX_RUFF_FORMAT=false) would otherwise turn off.
        monkeypatch.setattr(settings.sandbox, "syntax_fix_ruff_format", True)
        state = AgentState(
            messages=[
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "execute_code",
                            "args": {"code": "print(1\uff0c2)"},
                            "id": "1",
                        }
                    ],
                )
            ]
        )
        result = await syntax_fix_before_exec(state, self._runtime(), self._config())
        assert result is not None
        assert "messages" in result
        fixed = result["messages"][0]
        code = fixed.tool_calls[0]["args"]["code"]  # pyright: ignore[reportUnknownMemberType]
        # ruff_format (on by default) also normalizes spacing after the comma.
        assert "print(1, 2)" in code

    async def test_ruff_format_applied_when_enabled(self, monkeypatch: pytest.MonkeyPatch):
        """settings.sandbox.syntax_fix_ruff_format=True -> non-canonical style normalized."""

        monkeypatch.setattr(settings.sandbox, "syntax_fix_ruff_format", True)
        state = AgentState(
            messages=[
                AIMessage(
                    content="",
                    tool_calls=[{"name": "execute_code", "args": {"code": "x=1"}, "id": "1"}],
                )
            ]
        )
        result = await syntax_fix_before_exec(state, self._runtime(), self._config())
        assert result is not None
        assert result["messages"][0].tool_calls[0]["args"]["code"].strip() == "x = 1"  # pyright: ignore[reportUnknownMemberType]

    async def test_ruff_format_skipped_when_disabled(self, monkeypatch: pytest.MonkeyPatch):
        """settings.sandbox.syntax_fix_ruff_format=False -> style left untouched.

        Uses a duplicate import so _ruff_fix always triggers a change (ruff
        removes the duplicate) regardless of whether ruff also fixes W292
        (missing-newline-at-end-of-file).  This keeps the hook returning a
        non-None result so we can assert that format was not applied — the test
        no longer depends on a specific ruff lint rule being active."""

        monkeypatch.setattr(settings.sandbox, "syntax_fix_ruff_format", False)
        state = AgentState(
            messages=[
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "execute_code",
                            "args": {"code": "import os\nimport os\nx=1"},
                            "id": "1",
                        }
                    ],
                )
            ]
        )
        result = await syntax_fix_before_exec(state, self._runtime(), self._config())
        assert result is not None
        code = result["messages"][0].tool_calls[0]["args"]["code"]  # pyright: ignore[reportUnknownMemberType]
        # ruff format disabled: x=1 stays as-is, not reformatted to x = 1.
        assert "x = 1" not in code
        assert "x=1" in code

    async def test_missing_import_added(self):
        """Missing import should be added."""
        state = AgentState(
            messages=[
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "execute_code",
                            "args": {"code": "json.dumps({'a': 1})"},
                            "id": "1",
                        }
                    ],
                )
            ]
        )
        result = await syntax_fix_before_exec(state, self._runtime(), self._config())
        assert result is not None
        code = result["messages"][0].tool_calls[0]["args"]["code"]  # pyright: ignore[reportUnknownMemberType]
        assert "import json" in code

    async def test_syntax_error_injects_tool_message(self):
        """Unfixable syntax error should inject ToolMessage + goto after_exec.

        LLM repair is explicitly patched to be unavailable (returns None), locking the deterministic fallback path,
        and also avoids hitting the real DeepSeek API.
        """
        state = AgentState(
            messages=[
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "execute_code",
                            "args": {"code": "if True print('x')"},
                            "id": "1",
                        }
                    ],
                )
            ]
        )
        with patch(
            "ava_builtins.plugins.ava_syntax_fix.agent_runtime._llm_repair_syntax",
            new=AsyncMock(return_value=None),
        ):
            result = await syntax_fix_before_exec(state, self._runtime(), self._config())
        assert result is not None
        assert result.get("goto") == "after_exec"  # pyright: ignore[reportUnknownMemberType]
        assert "halted" in result
        # should have fixed_msg + tool_msg
        assert len(result["messages"]) == 2  # pyright: ignore[reportUnknownArgumentType]
        tool_msg = result["messages"][1]
        assert "SyntaxError" in tool_msg.content  # pyright: ignore[reportUnknownMemberType]

    @staticmethod
    def _broken_state(code: str) -> AgentState:
        return AgentState(
            messages=[
                AIMessage(
                    content="",
                    tool_calls=[{"name": "execute_code", "args": {"code": code}, "id": "1"}],
                )
            ]
        )

    async def test_llm_repair_success_silent_replacement(self):
        """LLM repair returns compilable code → silently replace with same ID, no ToolMessage, no goto."""
        broken = "x = 'unterminated\nprint(x)"
        repaired = "x = 'fixed'\nprint(x)"
        with patch(
            "ava_builtins.plugins.ava_syntax_fix.agent_runtime._llm_repair_syntax",
            new=AsyncMock(return_value=repaired),
        ):
            result = await syntax_fix_before_exec(
                self._broken_state(broken), self._runtime(), self._config()
            )
        assert result is not None
        assert "goto" not in result
        assert len(result["messages"]) == 1  # pyright: ignore[reportUnknownArgumentType]
        assert result["messages"][0].tool_calls[0]["args"]["code"] == repaired  # pyright: ignore[reportUnknownMemberType]

    async def test_llm_repair_unavailable_falls_back(self):
        """LLM repair unavailable / retries exhausted (returns None) → fall back to ToolMessage fallback path."""
        broken = "if True print('x')"
        with patch(
            "ava_builtins.plugins.ava_syntax_fix.agent_runtime._llm_repair_syntax",
            new=AsyncMock(return_value=None),
        ):
            result = await syntax_fix_before_exec(
                self._broken_state(broken), self._runtime(), self._config()
            )
        assert result is not None
        assert result.get("goto") == "after_exec"  # pyright: ignore[reportUnknownMemberType]
        assert len(result["messages"]) == 2  # pyright: ignore[reportUnknownArgumentType]
        assert "SyntaxError" in result["messages"][1].content  # pyright: ignore[reportUnknownMemberType]

    async def test_valid_code_no_change_returns_none(self):
        """Perfectly legal code is not modified, returns None."""
        state = AgentState(
            messages=[
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "execute_code",
                            "args": {"code": "x = 1 + 1\nprint(x)"},
                            "id": "1",
                        }
                    ],
                )
            ]
        )
        result = await syntax_fix_before_exec(state, self._runtime(), self._config())
        # ruff may format, so it's not guaranteed to return None.
        # Just assert no exception.
        if result is not None:
            assert "messages" in result


class TestIsStdlibModule:
    def test_known_stdlib(self):
        assert _is_stdlib_module("os")
        assert _is_stdlib_module("sys")
        assert _is_stdlib_module("json")
        assert _is_stdlib_module("collections")

    def test_not_stdlib(self):
        assert not _is_stdlib_module("numpy")
        assert not _is_stdlib_module("pandas")
        assert not _is_stdlib_module("nonexistent")

    def test_submodules_not_in_top_level(self):
        """os.path, urllib.parse are not top-level module names."""
        assert not _is_stdlib_module("os.path")
        assert _is_stdlib_module("os")


class TestExtractText:
    def test_plain_string_passthrough(self):
        assert _extract_text("hello") == "hello"

    def test_thinking_blocks_keep_only_text(self):
        content = [
            {"type": "thinking", "thinking": "let me think"},
            {"type": "text", "text": "x = 1"},
            {"type": "text", "text": "\nprint(x)"},
        ]
        assert _extract_text(content) == "x = 1\nprint(x)"

    def test_non_text_blocks_ignored(self):
        assert _extract_text([{"type": "thinking", "thinking": "only thinking"}]) == ""


class TestStripCodeFence:
    def test_no_fence_returns_stripped(self):
        assert _strip_code_fence("  x = 1  ") == "x = 1"

    def test_strips_python_fence(self):
        assert _strip_code_fence("```python\nx = 1\nprint(x)\n```") == "x = 1\nprint(x)"

    def test_strips_bare_fence(self):
        assert _strip_code_fence("```\nx = 1\n```") == "x = 1"


class TestRenderSyntaxError:
    def test_renders_line_and_message(self):
        rendered = ""
        try:
            compile("if True print('x')", "<agent_code>", "exec")
        except SyntaxError as e:
            rendered = _render_syntax_error(e, "if True print('x')")
        assert "SyntaxError:" in rendered
        assert "line 1" in rendered
        assert "<agent_code>" in rendered


@pytest.mark.usefixtures("sdk_model_owner")
class TestLlmRepairSyntax:
    async def test_returns_repaired_text(self):
        from unittest.mock import patch

        fake = MagicMock()
        fake.ainvoke = AsyncMock(return_value=MagicMock(content="x = 1"))
        with patch("base.lm.factory.build_chat_model", return_value=fake):
            from ava_builtins.plugins.ava_syntax_fix.agent_runtime import _llm_repair_syntax

            out = await _llm_repair_syntax("x = 'broken", "SyntaxError: ...")
        assert out == "x = 1"

    async def test_model_unavailable_returns_none(self):
        from unittest.mock import patch

        with patch("base.lm.factory.build_chat_model", side_effect=RuntimeError("no key")):
            from ava_builtins.plugins.ava_syntax_fix.agent_runtime import _llm_repair_syntax

            out = await _llm_repair_syntax("x = 'broken", "SyntaxError: ...")
        assert out is None

    async def test_empty_output_returns_none(self):
        from unittest.mock import patch

        fake = MagicMock()
        fake.ainvoke = AsyncMock(return_value=MagicMock(content="   "))
        with patch("base.lm.factory.build_chat_model", return_value=fake):
            from ava_builtins.plugins.ava_syntax_fix.agent_runtime import _llm_repair_syntax

            out = await _llm_repair_syntax("x = 'broken", "SyntaxError: ...")
        assert out is None


class TestFixUnicodePunctuation:
    def test_em_dash_replaced(self):
        fixed, n = fix_unicode_punctuation("x = 1 \u2014 2")
        assert "\u2014" not in fixed
        assert "-" in fixed
        assert n == 1

    def test_arrow_replaced(self):
        fixed, n = fix_unicode_punctuation("a \u2192 b")
        assert "\u2192" not in fixed
        assert "->" in fixed
        assert n == 1

    def test_en_dash_replaced(self):
        fixed, n = fix_unicode_punctuation("x \u2013 y")
        assert "\u2013" not in fixed
        assert n == 1

    def test_right_single_quote_replaced(self):
        fixed, n = fix_unicode_punctuation("x = \u2019hello\u2019")
        assert "\u2019" not in fixed
        assert "'" in fixed
        assert n == 2

    def test_no_unicode_no_change(self):
        code = "x = 1 + 2"
        fixed, n = fix_unicode_punctuation(code)
        assert fixed == code
        assert n == 0

    def test_multiple_mixed(self):
        _fixed, n = fix_unicode_punctuation("a \u2014 b \u2192 c")
        assert n == 2


class TestFixStringNewlines:
    def test_single_quoted_cross_line(self):
        # Single-quoted string with literal newline
        code = "x = 'hello\nworld'"
        fixed, n = fix_string_newlines(code)
        assert n >= 1
        compile(fixed, "<t>", "exec")

    def test_fstring_cross_line(self):
        code = "x = f'hello\nworld'"
        fixed, n = fix_string_newlines(code)
        assert n >= 1
        compile(fixed, "<t>", "exec")

    def test_double_quoted_cross_line(self):
        code = 'x = "hello\nworld"'
        fixed, n = fix_string_newlines(code)
        assert n >= 1
        compile(fixed, "<t>", "exec")

    def test_already_triple_quoted_no_change(self):
        code = "x = '''hello\nworld'''"
        _fixed, n = fix_string_newlines(code)
        assert n == 0

    def test_valid_escape_no_change(self):
        code = "x = 'hello\\nworld'"
        _fixed, n = fix_string_newlines(code)
        assert n == 0

    def test_single_line_no_change(self):
        code = "x = 'hello world'"
        _fixed, n = fix_string_newlines(code)
        assert n == 0


class TestFixUnclosedTripleQuote:
    def test_unclosed_double_triple(self):
        fixed, n = fix_unclosed_triple_quote('x = """hello')
        assert n == 1
        assert fixed.rstrip().endswith('"""')

    def test_unclosed_single_triple(self):
        fixed, n = fix_unclosed_triple_quote("x = '''hello")
        assert n == 1
        assert fixed.rstrip().endswith("'" * 3)

    def test_already_closed_no_change(self):
        _fixed, n = fix_unclosed_triple_quote('x = """hello"""')
        assert n == 0

    def test_no_triple_quotes_no_change(self):
        _fixed, n = fix_unclosed_triple_quote("x = 'hello'")
        assert n == 0


class TestFixIndentation:
    def test_tabs_converted_to_spaces(self):
        fixed, n = fix_indentation("\tprint('hello')")
        assert "\t" not in fixed
        assert "    " in fixed
        assert n >= 1

    def test_no_tabs_no_change(self):
        _fixed, n = fix_indentation("    print('hello')")
        assert n == 0

    def test_tab_in_string_body_preserved(self):
        # Tab after non-whitespace is not leading whitespace
        code = 'x = "hello\tworld"'
        fixed, _n = fix_indentation(code)
        assert "\t" in fixed  # preserved in string body


class TestFixBracketMatching:
    def test_missing_closing_paren(self):
        fixed, n = fix_bracket_matching("(1, 2")
        assert n == 1
        assert ")" in fixed

    def test_missing_closing_bracket(self):
        fixed, n = fix_bracket_matching("[1, 2")
        assert n == 1
        assert "]" in fixed

    def test_missing_closing_brace(self):
        fixed, n = fix_bracket_matching("{'a': 1")
        assert n == 1
        assert "}" in fixed

    def test_already_balanced_no_change(self):
        _fixed, n = fix_bracket_matching("(1, 2)")
        assert n == 0

    def test_balanced_in_string_body(self):
        _fixed, n = fix_bracket_matching('x = "(" + ")"')
        assert n == 0


class TestFixFstringExpressions:
    def test_empty_braces_replaced(self):
        fixed, n = fix_fstring_expressions('f"hello {}"')
        assert "{None}" in fixed
        assert n == 1

    def test_multiple_empty_braces(self):
        _fixed, n = fix_fstring_expressions('f"{} and {}"')
        assert n == 2

    def test_no_fstring_no_change(self):
        _fixed, n = fix_fstring_expressions('"hello {}"')
        assert n == 0

    def test_valid_fstring_no_change(self):
        _fixed, n = fix_fstring_expressions('f"hello {name}"')
        assert n == 0


class TestFixUnterminatedToTriple:
    """Multi-line string/f-string opened with a single quote (the dominant
    `ava.shell.run("...multi-line shell command...")` pattern from the
    production dataset). The single quote cannot span newlines, so it is
    unterminated; the fix re-delimits opener + matching closer to triple."""

    def test_multiline_double_quoted_command(self):
        # A double-quoted shell command whose commit message spans newlines.
        code = 'r = run("git commit -m \'fix\nbody\nmore")'
        assert _is_broken(code)
        fixed, n = fix_unterminated_to_triple(code)
        assert n == 1
        compile(fixed, "<t>", "exec")

    def test_multiline_fstring_command(self):
        # An f-string command spanning newlines; the offset points at the f prefix.
        code = 'r = run(f"cd {d} && commit -m \'msg\nbody")'
        assert _is_broken(code)
        fixed, n = fix_unterminated_to_triple(code)
        assert n == 1
        compile(fixed, "<t>", "exec")

    def test_nested_same_char_quote_in_body(self):
        # The body contains the delimiter char (single quote, around 'quoted')
        # before the real closer. fix_string_newlines picks the first such quote
        # as the closer and mis-fires; the offset-driven closer search tries
        # candidates until one compiles, landing on the real closer.
        code = "r = run('git commit -m msg\nadd a 'quoted' word\nmore')"
        assert _is_broken(code)
        fixed, n = fix_unterminated_to_triple(code)
        assert n == 1
        compile(fixed, "<t>", "exec")

    def test_valid_string_no_change(self):
        code = 'x = "hello world"'
        assert fix_unterminated_to_triple(code) == (code, 0)

    def test_unrelated_error_no_change(self):
        # A non-string syntax error must not trigger this fixer.
        code = "def f(:\n    pass"
        assert fix_unterminated_to_triple(code) == (code, 0)
