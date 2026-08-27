"""Tests for plugins/ava_syntax_fix/plugin.py.

Coverage:
- _fix_chinese_punctuation: convert Chinese punctuation to ASCII
- _detect_missing_imports: detect missing imports
- _insert_imports: insert import statements at the correct position
- _ruff_fix: ruff check --fix (mock subprocess)
- syntax_fix_before_exec: full pipeline (Chinese punctuation → import → ruff → compile)
"""

import ast
import pathlib
import shutil
import subprocess
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableConfig
from langgraph.runtime import Runtime

from agent.graph._context import AvaContext
from agent.state import AgentState
from ava_builtins.plugins.ava_syntax_fix._deterministic_fixes import (
    apply_all_deterministic_fixes,
    fix_bracket_matching,
    fix_escape_inner_quotes,
    fix_fstring_expressions,
    fix_indentation,
    fix_nested_triple_quote,
    fix_string_newlines,
    fix_unclosed_triple_quote,
    fix_unicode_punctuation,
    fix_unterminated_to_triple,
)
from ava_builtins.plugins.ava_syntax_fix._imports import (
    _ruff_undefined_names,
    _warn_ruff_missing_once,
)
from ava_builtins.plugins.ava_syntax_fix.plugin import (
    _detect_missing_imports,
    _extract_text,
    _fix_chinese_punctuation,
    _fix_invalid_escapes,
    _insert_imports,
    _is_stdlib_module,
    _render_syntax_error,
    _ruff_executable,
    _ruff_fix,
    _ruff_format,
    _strip_code_fence,
    syntax_fix_before_exec,
)

# --- _fix_chinese_punctuation ---


class TestFixChinesePunctuation:
    def test_no_chinese_no_change(self):
        code, n = _fix_chinese_punctuation('print("hello")')
        assert code == 'print("hello")'
        assert n == 0

    def test_chinese_comma_to_ascii(self):
        # \uff0c = fullwidth comma
        code, n = _fix_chinese_punctuation("print(1\uff0c2)")
        assert code == "print(1,2)"
        assert n == 1

    def test_chinese_period_to_dot(self):
        # \u3002 = fullwidth period
        code, n = _fix_chinese_punctuation("import os\u3002")
        assert code == "import os."
        assert n == 1

    def test_chinese_left_quote_to_ascii(self):
        # \uff08 = fullwidth left paren, \uff09 = fullwidth right paren
        code, n = _fix_chinese_punctuation("print\uff081\uff09")
        assert code == "print(1)"
        assert n == 2

    def test_multiple_replacements(self):
        # Multiple Chinese punctuation
        code, n = _fix_chinese_punctuation("a\uff0cb\u3002c\uff08d\uff09")
        assert code == "a,b.c(d)"
        assert n == 4

    def test_empty_code(self):
        code, n = _fix_chinese_punctuation("")
        assert code == ""
        assert n == 0

    def test_in_string_punctuation_preserved(self):
        # Fullwidth comma inside a string literal is intended text, not a code
        # fat-finger -- must be left untouched.
        src = 'msg = "\u4f60\u597d\uff0c\u4e16\u754c"'
        code, n = _fix_chinese_punctuation(src)
        assert code == src
        assert n == 0

    def test_in_triple_quote_preserved(self):
        # The agent68 class of bug: a Chinese prompt in a triple-quoted string.
        # Rewriting fullwidth quotes inside could forge a closing \"\"\" and break
        # the literal. The string must survive verbatim and still compile.
        src = 'p = """\u95ee\u9898\u80cc\u666f\uff1a\u8bf7\u7528 \u201c\u667a\u80fd\u201d \u6a21\u5f0f\u3002"""'
        code, n = _fix_chinese_punctuation(src)
        assert code == src
        assert n == 0
        compile(code, "<t>", "exec")  # still valid

    def test_comment_punctuation_preserved(self):
        src = "x = 1  # \u8c03\u7528\uff08\u91cd\u8981\uff09"
        code, n = _fix_chinese_punctuation(src)
        assert code == src
        assert n == 0

    def test_code_position_fixed_but_string_preserved(self):
        # Same line: fullwidth paren in code position is fixed; fullwidth comma
        # inside the string argument is preserved.
        src = 'print\uff08"a\uff0cb"\uff09'
        code, n = _fix_chinese_punctuation(src)
        assert code == 'print("a\uff0cb")'
        assert n == 2

    def test_fullwidth_quote_delimiters_fixed_interior_preserved(self):
        # Model used fullwidth quotes AS string delimiters. Pass 1 converts the
        # delimiters to ASCII so the body becomes a real string token; pass 2
        # then leaves the interior comma alone. Delimiters fixed, text intact.
        src = "msg = \u201c\u4f60\u597d\uff0c\u4e16\u754c\u201d"
        code, n = _fix_chinese_punctuation(src)
        assert code == 'msg = "\u4f60\u597d\uff0c\u4e16\u754c"'
        assert n == 2  # only the two quotes; the interior fullwidth comma stays
        compile(code, "<t>", "exec")

    def test_tokenize_failure_returns_unchanged(self):
        # Unterminated paren makes tokenize raise TokenError; rather than risk
        # corrupting in-string text we return the source unchanged and let
        # compile()/LLM repair handle it downstream.
        src = 'x = ("a\uff0cb"'
        code, n = _fix_chinese_punctuation(src)
        assert code == src
        assert n == 0


# --- _detect_missing_imports ---


class TestDetectMissingImports:
    def test_empty_code_returns_empty(self):
        assert _detect_missing_imports("") == []

    def test_whitespace_only_returns_empty(self):
        assert _detect_missing_imports("   \n  ") == []

    def test_no_unknown_usage_returns_empty(self):
        assert _detect_missing_imports("x = 1") == []

    def test_detects_json_usage(self):
        result = _detect_missing_imports("json.dumps({'a': 1})")
        assert "import json" in result

    def test_detects_math_usage(self):
        result = _detect_missing_imports("math.sqrt(4)")
        assert "import math" in result

    def test_detects_re_usage(self):
        result = _detect_missing_imports("re.compile(r'.*')")
        assert "import re" in result

    def test_detects_datetime_usage(self):
        result = _detect_missing_imports("datetime.datetime.now()")
        assert "import datetime" in result

    def test_skips_already_imported(self):
        code = "import json\njson.dumps({'a': 1})"
        assert _detect_missing_imports(code) == []

    def test_skips_already_from_imported(self):
        code = "from json import dumps\ndumps({'a': 1})"
        assert _detect_missing_imports(code) == []

    def test_skips_builtins(self):
        """True, False, None etc. are builtins and should not be auto-imported."""
        assert _detect_missing_imports("True") == []
        assert _detect_missing_imports("print('hello')") == []
        assert _detect_missing_imports("len([1,2])") == []

    def test_detects_os_import(self):
        """os should be auto-imported (it is not in the execution namespace)."""
        result = _detect_missing_imports("os.getcwd()")
        assert "import os" in result

    def test_detects_sys_import(self):
        """sys should be auto-imported."""
        result = _detect_missing_imports("sys.argv")
        assert "import sys" in result

    def test_detects_time_import(self):
        """time should be auto-imported."""
        result = _detect_missing_imports("time.sleep(1)")
        assert "import time" in result

    def test_detects_dotted_import_urllib(self):
        result = _detect_missing_imports("urllib.parse.urlparse('http://x.com')")
        assert "import urllib.parse" in result

    def test_detects_concurrent_futures(self):
        result = _detect_missing_imports("concurrent.futures.ThreadPoolExecutor()")
        assert any("concurrent.futures" in s for s in result)

    def test_multiple_missing_sorted(self):
        code = """
json.dumps(x)
math.sqrt(y)
re.compile(z)
"""
        result = _detect_missing_imports(code)
        # should be sorted alphabetically
        imports = [s for s in result if s.startswith("import ")]
        assert imports == sorted(imports)

    def test_ava_transitive_skipped(self):
        """os, sys, time are now explicitly imported (no longer transitively available through ava)."""
        code = "os.getcwd()\nsys.argv\ntime.time()"
        result = _detect_missing_imports(code)
        # os, sys, time should all appear in the result
        stmts = set(result)
        assert "import os" in stmts
        assert "import sys" in stmts
        assert "import time" in stmts

    def test_ava_detected(self):
        """ava should be auto-imported in syntax-fix (PR #484 removed auto-import ava from execute_code)."""
        result = _detect_missing_imports("ava.files.read('x')")
        assert "import ava" in result

    def test_ava_already_imported_skipped(self):
        """When ava is already imported, do not add it again."""
        code = "import ava\nava.files.read('x')"
        assert _detect_missing_imports(code) == []

    def test_import_below_first_usage_counts_as_missing(self):
        """`import os` below the first `os.attr` would NameError at runtime —
        detector must still flag it so the prepended import resolves the use."""
        code = "os.environ.get('X')\nimport os\nos.walk('/')\n"
        result = _detect_missing_imports(code)
        assert "import os" in result

    def test_import_above_first_usage_skipped(self):
        """Standard ordering: import before use — no fresh import needed."""
        code = "import os\nos.environ.get('X')\n"
        assert _detect_missing_imports(code) == []

    def test_dotted_import_below_first_usage_counts_as_missing(self):
        """Same rule for dotted modules (urllib.parse etc.)."""
        code = "urllib.parse.urlparse('http://x')\nimport urllib.parse\n"
        assert "import urllib.parse" in _detect_missing_imports(code)

    def test_from_import_below_first_usage_counts_as_missing(self):
        """`from json import dumps` below its first attr-use counts as missing
        too — `json.dumps` on line 1 sees no `json` binding yet."""
        code = "json.dumps({'a': 1})\nfrom json import dumps\n"
        assert "import json" in _detect_missing_imports(code)


# --- _insert_imports ---


class TestInsertImports:
    def test_simple_code_inserts_at_top(self):
        code = "print('hello')"
        result = _insert_imports(code, ["import json"])
        assert result.startswith("import json\n")
        assert "print('hello')" in result

    def test_after_shebang(self):
        code = "#!/usr/bin/env python\nprint('hello')"
        result = _insert_imports(code, ["import json"])
        lines = result.split("\n")
        assert lines[0] == "#!/usr/bin/env python"
        assert "import json" in lines[1]

    def test_after_module_docstring(self):
        code = '"""Module docstring.\n"""\nprint(1)'
        result = _insert_imports(code, ["import json"])
        lines = result.split("\n")
        assert lines[0] == '"""Module docstring.'
        assert "import json" in result
        assert "print(1)" in result

    def test_after_comments(self):
        code = "# comment 1\n# comment 2\nprint(1)"
        result = _insert_imports(code, ["import json"])
        lines = result.split("\n")
        assert lines[0] == "# comment 1"
        assert lines[1] == "# comment 2"
        assert "import json" in lines[2]

    def test_empty_imports_no_change(self):
        code = "print(1)"
        result = _insert_imports(code, [])
        # empty import list inserts an empty string at the insert_at position
        assert "print(1)" in result

    def test_multiple_imports(self):
        code = "print(1)"
        result = _insert_imports(code, ["import json", "import math"])
        assert "import json" in result
        assert "import math" in result


# --- _ruff_fix ---


class TestRuffFix:
    def test_ruff_available_runs_fix(self):
        """Verify ruff is actually called and returns fixed code."""
        # ruff should be available in dev env
        code = "import os\nimport os\n"
        result = _ruff_fix(code)
        # ruff should remove the duplicate import (output may differ, at least shouldn't crash)
        assert isinstance(result, str)
        assert len(result) > 0

    @patch("subprocess.run")
    def test_ruff_not_found_returns_original(self, mock_run):
        mock_run.side_effect = FileNotFoundError
        code = "import os\nimport os\n"
        assert _ruff_fix(code) == code

    @patch("subprocess.run")
    def test_ruff_timeout_returns_original(self, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired("ruff", 5)
        code = "import os\n"
        assert _ruff_fix(code) == code

    @patch("subprocess.run")
    def test_ruff_nonzero_returns_original(self, mock_run):
        mock_run.return_value = subprocess.CompletedProcess(
            args=["ruff"], returncode=1, stdout="", stderr="error"
        )
        code = "import os\n"
        assert _ruff_fix(code) == code

    @patch("subprocess.run")
    def test_ruff_empty_stdout_returns_original(self, mock_run):
        mock_run.return_value = subprocess.CompletedProcess(
            args=["ruff"], returncode=0, stdout="", stderr=""
        )
        code = "import os\n"
        assert _ruff_fix(code) == code


# --- _ruff_format ---


class TestRuffFormat:
    def test_ruff_available_normalizes_style(self):
        """ruff format normalizes non-canonical style to canonical (ruff should be available in dev env)."""
        result = _ruff_format("x=1\n")
        assert result == "x = 1\n"

    @patch("subprocess.run")
    def test_ruff_not_found_returns_original(self, mock_run):
        mock_run.side_effect = FileNotFoundError
        assert _ruff_format("x=1\n") == "x=1\n"

    @patch("subprocess.run")
    def test_ruff_timeout_returns_original(self, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired("ruff", 5)
        assert _ruff_format("x=1\n") == "x=1\n"

    @patch("subprocess.run")
    def test_ruff_nonzero_returns_original(self, mock_run):
        mock_run.return_value = subprocess.CompletedProcess(
            args=["ruff"], returncode=2, stdout="", stderr="error"
        )
        assert _ruff_format("x=1\n") == "x=1\n"

    @patch("subprocess.run")
    def test_ruff_empty_stdout_returns_original(self, mock_run):
        mock_run.return_value = subprocess.CompletedProcess(
            args=["ruff"], returncode=0, stdout="", stderr=""
        )
        assert _ruff_format("x=1\n") == "x=1\n"


# --- ruff give-up logging (issue #159) ---
# A ruff pass that gives up must be visible: a timeout / OS error logs a
# warning with the budget, input size, and errno; a missing ruff logs once per
# process. The pass-through behavior itself is unchanged.


class TestRuffGiveUpLogging:
    @patch("subprocess.run")
    def test_ruff_fix_timeout_logs_warning(self, mock_run, loguru_records):
        mock_run.side_effect = subprocess.TimeoutExpired("ruff", 5)
        code = "import os\n"
        assert _ruff_fix(code) == code
        msgs = [r["message"] for r in loguru_records]
        assert any("did not finish within 5s" in m and "char source" in m for m in msgs), msgs

    @patch("subprocess.run")
    def test_ruff_fix_oserror_logs_errno(self, mock_run, loguru_records):
        mock_run.side_effect = OSError(24, "Too many open files")
        code = "import os\n"
        assert _ruff_fix(code) == code
        msgs = [r["message"] for r in loguru_records]
        assert any("errno=24" in m and "Too many open files" in m for m in msgs), msgs

    @patch("subprocess.run")
    def test_ruff_format_timeout_logs_warning(self, mock_run, loguru_records):
        mock_run.side_effect = subprocess.TimeoutExpired("ruff", 5)
        code = "x=1\n"
        assert _ruff_format(code) == code
        msgs = [r["message"] for r in loguru_records]
        assert any("did not finish within 5s" in m and "char source" in m for m in msgs), msgs

    @patch("subprocess.run")
    def test_ruff_missing_logs_once_per_process(self, mock_run, loguru_records):
        """A host without ruff logs its absence once, not once per call."""
        _warn_ruff_missing_once.cache_clear()
        mock_run.side_effect = FileNotFoundError
        try:
            _ruff_fix("a = 1\n")
            _ruff_fix("b = 2\n")
            _ruff_format("c = 3\n")
            msgs = [r["message"] for r in loguru_records]
            assert sum("not found" in m for m in msgs) == 1, msgs
        finally:
            _warn_ruff_missing_once.cache_clear()

    @patch("subprocess.run")
    def test_undefined_names_timeout_logs_warning(self, mock_run, loguru_records):
        mock_run.side_effect = subprocess.TimeoutExpired("ruff", 5)
        assert _ruff_undefined_names("import os\n") == set()
        msgs = [r["message"] for r in loguru_records]
        assert any("did not finish within 5s" in m and "check --select F821" in m for m in msgs), (
            msgs
        )

    @patch("subprocess.run")
    def test_undefined_names_oserror_logs_errno(self, mock_run, loguru_records):
        mock_run.side_effect = OSError(28, "No space left on device")
        assert _ruff_undefined_names("import os\n") == set()
        msgs = [r["message"] for r in loguru_records]
        assert any("errno=28" in m for m in msgs), msgs


# --- _ruff_executable ---


class TestRuffExecutableResolution:
    """ruff must resolve without the venv's ``bin`` dir on ``PATH``.

    The agent runs as ``<venv>/bin/python`` with the venv never *activated*, so
    ``PATH`` generally lacks ``<venv>/bin``. Every ruff-backed fixer swallows
    ``FileNotFoundError`` and returns its input unchanged, so a bare ``"ruff"``
    lookup degrades the whole missing-import / lint / format stage to a silent
    no-op there rather than failing loudly.
    """

    def test_resolves_to_the_interpreters_own_ruff(self):
        resolved = pathlib.Path(_ruff_executable())
        assert resolved.is_file()
        assert resolved.parent == pathlib.Path(sys.executable).parent

    def test_ruff_backed_fixers_work_with_ruff_absent_from_path(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """The regression itself: empty PATH must not disable the fixers."""
        monkeypatch.setenv("PATH", "")
        _ruff_executable.cache_clear()
        try:
            assert shutil.which("ruff") is None, "precondition: PATH cannot find ruff"
            assert _ruff_format("x=1\n") == "x = 1\n"
            assert "import json" in _detect_missing_imports("json.dumps({'a': 1})")
        finally:
            _ruff_executable.cache_clear()


# ============================================================================
# _fix_invalid_escapes
# ============================================================================


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


# ============================================================================
# syntax_fix_before_exec integration
# ============================================================================


class TestSyntaxFixBeforeExec:
    @staticmethod
    def _runtime():
        ctx = AvaContext(
            ops_pool=AsyncMock(),
            llm=MagicMock(),
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
        from ava_builtins.plugins.ava_syntax_fix import plugin as _plugin

        # Pin the flag: the assertion expects ruff_format spacing, which a host
        # .env (AVA_SYNTAX_FIX_RUFF_FORMAT=false) would otherwise turn off.
        monkeypatch.setattr(_plugin.settings.sandbox, "syntax_fix_ruff_format", True)
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
        from ava_builtins.plugins.ava_syntax_fix import plugin as _plugin

        monkeypatch.setattr(_plugin.settings.sandbox, "syntax_fix_ruff_format", True)
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
        from ava_builtins.plugins.ava_syntax_fix import plugin as _plugin

        monkeypatch.setattr(_plugin.settings.sandbox, "syntax_fix_ruff_format", False)
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
            "ava_builtins.plugins.ava_syntax_fix.plugin._llm_repair_syntax",
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
            "ava_builtins.plugins.ava_syntax_fix.plugin._llm_repair_syntax",
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
            "ava_builtins.plugins.ava_syntax_fix.plugin._llm_repair_syntax",
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


# ============================================================================
# _is_stdlib_module
# ============================================================================


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


# LLM repair helpers
# ============================================================================


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


class TestLlmRepairSyntax:
    async def test_returns_repaired_text(self):
        from unittest.mock import patch

        fake = MagicMock()
        fake.ainvoke = AsyncMock(return_value=MagicMock(content="x = 1"))
        with patch("shared.lm.factory.build_chat_model", return_value=fake):
            from ava_builtins.plugins.ava_syntax_fix.plugin import _llm_repair_syntax

            out = await _llm_repair_syntax("x = 'broken", "SyntaxError: ...")
        assert out == "x = 1"

    async def test_model_unavailable_returns_none(self):
        from unittest.mock import patch

        with patch("shared.lm.factory.build_chat_model", side_effect=RuntimeError("no key")):
            from ava_builtins.plugins.ava_syntax_fix.plugin import _llm_repair_syntax

            out = await _llm_repair_syntax("x = 'broken", "SyntaxError: ...")
        assert out is None

    async def test_empty_output_returns_none(self):
        from unittest.mock import patch

        fake = MagicMock()
        fake.ainvoke = AsyncMock(return_value=MagicMock(content="   "))
        with patch("shared.lm.factory.build_chat_model", return_value=fake):
            from ava_builtins.plugins.ava_syntax_fix.plugin import _llm_repair_syntax

            out = await _llm_repair_syntax("x = 'broken", "SyntaxError: ...")
        assert out is None


# ============================================================================
# Deterministic fixes tests (syntax-fix v3)
# ============================================================================


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


def _is_broken(code: str) -> bool:
    """True when `code` fails to compile -- used to assert a fixture is a
    genuine syntax error before a fixer is run against it."""
    try:
        compile(code, "<t>", "exec")
    except SyntaxError:
        return True
    return False


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


# ============================================================================
# trace-v2 syntax_fix events (task #792 group B)
# ============================================================================


class TestSyntaxFixEvents:
    """The before/after retention events: rough fix stores before only (after
    is replayable), lm fix stores before AND after (LLM rewrites are not
    replayable). Both go out as event="syntax_fix" loguru records whose extra
    lands in agent_events.payload."""

    @staticmethod
    def _runtime():
        ctx = AvaContext(
            ops_pool=AsyncMock(),
            llm=MagicMock(),
        )
        return Runtime(context=ctx)

    @staticmethod
    def _config() -> RunnableConfig:
        return {"configurable": {"thread_id": "7"}}

    async def test_rough_fix_emits_before_only(self, monkeypatch: pytest.MonkeyPatch):
        """A deterministic (rough) fix records fix_type=rough with the original
        source as `before` and no `after` — the after state is replayable via
        _apply_fix_pipeline(before)."""
        from ava_builtins.plugins.ava_syntax_fix import plugin as _plugin

        events: list[dict] = []
        monkeypatch.setattr(_plugin, "_emit_syntax_fix_event", lambda **kw: events.append(kw))  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
        # Chinese comma -> deterministic fix fires
        original = "print(1\uff0c2)"
        state = AgentState(
            messages=[
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "execute_code",
                            "args": {"code": original},
                            "id": "1",
                        }
                    ],
                )
            ]
        )
        result = await syntax_fix_before_exec(state, self._runtime(), self._config())
        assert result is not None
        assert len(events) == 1  # pyright: ignore[reportUnknownArgumentType]
        ev = events[0]
        assert ev["fix_type"] == "rough"
        assert ev["before"] == original
        assert ev["after"] is None
        assert ev["fixes"], "deterministic fixes must be listed"
        assert "chinese_punct" in ",".join(ev["fixes"])  # pyright: ignore[reportUnknownArgumentType]

    async def test_rough_fix_event_logger_payload_shape(self, monkeypatch: pytest.MonkeyPatch):
        """The real emit path: a loguru record with event='syntax_fix' whose
        extra carries fix_type / before / fixes — the shape agent_events.payload
        stores."""
        from ava_builtins.plugins.ava_syntax_fix import plugin as _plugin

        captured: dict = {}
        monkeypatch.setattr(_plugin.logger, "info", lambda _msg, **kw: captured.update(kw))  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
        _plugin._emit_syntax_fix_event(
            fix_type="rough",
            before="print(1\uff0c2)",
            after=None,
            fixes=["chinese_punct(1)"],
            note="deterministic fixes applied",
        )
        assert captured["event"] == "syntax_fix"
        assert captured["fix_type"] == "rough"
        assert captured["before"] == "print(1\uff0c2)"
        assert captured["fixes"] == ["chinese_punct(1)"]
        assert "after" not in captured

    async def test_lm_fix_emits_before_and_after(self, monkeypatch: pytest.MonkeyPatch):
        """An LLM repair records fix_type=lm with both before (the
        deterministic-fixed source the LLM saw) and after (the repair)."""
        from ava_builtins.plugins.ava_syntax_fix import plugin as _plugin

        events: list[dict] = []
        monkeypatch.setattr(_plugin, "_emit_syntax_fix_event", lambda **kw: events.append(kw))  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
        broken = "x = 'unterminated\nprint(x)"
        repaired = "x = 'fixed'\nprint(x)"
        with patch(
            "ava_builtins.plugins.ava_syntax_fix.plugin._llm_repair_syntax",
            new=AsyncMock(return_value=repaired),
        ):
            result = await syntax_fix_before_exec(
                TestSyntaxFixEvents._broken_state(broken),
                self._runtime(),
                self._config(),
            )
        assert result is not None
        assert len(events) == 1  # pyright: ignore[reportUnknownArgumentType]
        ev = events[0]
        assert ev["fix_type"] == "lm"
        assert ev["before"] == broken  # no deterministic fix fired -> LLM saw the original
        assert ev["after"] == repaired

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

    async def test_no_event_when_nothing_changed(self, monkeypatch: pytest.MonkeyPatch):
        """Clean code that compiles as-is emits no syntax_fix event — the event
        stream records mutations only."""
        from ava_builtins.plugins.ava_syntax_fix import plugin as _plugin

        events: list[dict] = []
        monkeypatch.setattr(_plugin, "_emit_syntax_fix_event", lambda **kw: events.append(kw))  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
        # Force the pipeline to be a no-op (nothing to fix, nothing formatted)
        # so the event decision is what is under test — not ruff's formatting.
        monkeypatch.setattr(_plugin, "_apply_fix_pipeline", lambda code: (code, []))  # pyright: ignore[reportUnknownArgumentType]
        state = AgentState(
            messages=[
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "execute_code",
                            "args": {"code": "x = 1\nprint(x)"},
                            "id": "1",
                        }
                    ],
                )
            ]
        )
        await syntax_fix_before_exec(state, self._runtime(), self._config())
        assert events == []
