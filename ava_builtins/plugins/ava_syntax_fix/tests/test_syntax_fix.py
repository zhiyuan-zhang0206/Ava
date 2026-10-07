"""Tests for plugins/ava_syntax_fix/plugin.py.

Coverage:
- _fix_chinese_punctuation: convert Chinese punctuation to ASCII
- _detect_missing_imports: detect missing imports
- _insert_imports: insert import statements at the correct position
- _ruff_fix: ruff check --fix (mock subprocess)
- syntax_fix_before_exec: full pipeline (Chinese punctuation → import → ruff → compile)
"""

import pathlib
import shutil
import subprocess
import sys
from unittest.mock import MagicMock, patch

import pytest

from ava_builtins.plugins.ava_syntax_fix._imports import (
    _ruff_undefined_names,
    _warn_ruff_missing_once,
)
from ava_builtins.plugins.ava_syntax_fix._punct import (
    _FULLWIDTH_QUOTE_MAP,
    _MAX_TOKENIZE_LINE_LENGTH,
    _PUNCT_MAP,
    _translate_outside_strings,
)
from ava_builtins.plugins.ava_syntax_fix.agent_runtime import (
    _detect_missing_imports,
    _fix_chinese_punctuation,
    _insert_imports,
    _ruff_executable,
    _ruff_fix,
    _ruff_format,
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


# --- _translate_outside_strings crash guard (gh-149183) ---


class TestTranslateOutsideStringsCrashGuard:
    """The CPython 3.12+ C tokenizer crashes instead of raising a tokenize
    error on f-string replacement fields that mix '=' (debug), ':'/'!'
    delimiters and invalid expressions: it computes a negative string length
    and raises SystemError("Negative size passed to PyUnicode_New")
    (gh-149183; upstream fix gh-149445 targets 3.15+ only; on 3.13+ the same
    input surfaces as MemoryError -- the guard degrades both). The
    punctuation translation must degrade to a no-op instead of aborting the
    agent host run.
    """

    # Minimal gh-149183-style trigger, distilled from the 2026-09-09 agent
    # 3428 incident input: '=' in the replacement field enables the debug
    # metadata path, and the two '!' attribute expressions desync the
    # expression buffer offsets so the C tokenizer computes a negative size.
    CRASH_INPUT = (
        "x = f'''() => {\n"
        "      x = 1;\n"
        "      return {height: r.height,\n"
        "              brandAbsent: !h.query('a') && !h.query('b'),\n"
        '    }""")\n'
        "'''"
    )

    @pytest.mark.skipif(
        sys.version_info < (3, 12),
        reason="the guarded C-tokenizer crash exists on 3.12+ (SystemError on "
        "3.12, MemoryError on 3.13+; the guard degrades both)",
    )
    def test_crash_input_degrades_to_noop_quote_pass(self):
        code, n = _translate_outside_strings(self.CRASH_INPUT, _FULLWIDTH_QUOTE_MAP)
        assert code == self.CRASH_INPUT
        assert n == 0

    @pytest.mark.skipif(
        sys.version_info < (3, 12),
        reason="the guarded C-tokenizer crash exists on 3.12+ (SystemError on "
        "3.12, MemoryError on 3.13+; the guard degrades both)",
    )
    def test_crash_input_degrades_to_noop_punct_pass(self):
        code, n = _translate_outside_strings(self.CRASH_INPUT, _PUNCT_MAP)
        assert code == self.CRASH_INPUT
        assert n == 0

    @pytest.mark.skipif(
        sys.version_info < (3, 12),
        reason="3.11 tokenizes the input (and would translate the fullwidth "
        "comma); the guard degrades the 3.12+ crash manifestations",
    )
    def test_pipeline_degrades_on_crash_input(self):
        src = self.CRASH_INPUT.replace("() => {", "() => {\uff0c")
        code, n = _fix_chinese_punctuation(src)
        assert code == src
        assert n == 0

    @pytest.mark.skipif(
        sys.version_info < (3, 12),
        reason="3.11 tokenizes the input (and would translate the fullwidth "
        "comma); the guard degrades the 3.12+ crash manifestations",
    )
    def test_escapes_fixer_degrades_on_crash_input(self):
        # The escape fixer is the next in-process tokenize stage; the same
        # input must degrade there too instead of crashing the pipeline.
        from ava_builtins.plugins.ava_syntax_fix._escapes import _fix_invalid_escapes

        code, n = _fix_invalid_escapes(self.CRASH_INPUT)
        assert code == self.CRASH_INPUT
        assert n == 0

    def test_memoryerror_from_tokenizer_degrades_to_noop(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # 3.13+ surfaces the gh-149183 input as MemoryError; the guard must
        # degrade that too instead of aborting the agent host run. Injected
        # so the branch runs on every interpreter (CI pins 3.12, where the
        # real input raises SystemError).
        import tokenize

        def boom(_readline: object) -> object:
            raise MemoryError("simulated C-tokenizer MemoryError")

        monkeypatch.setattr(tokenize, "generate_tokens", boom)
        code, n = _translate_outside_strings(self.CRASH_INPUT, _PUNCT_MAP)
        assert code == self.CRASH_INPUT
        assert n == 0

        from ava_builtins.plugins.ava_syntax_fix._escapes import _fix_invalid_escapes

        esc, esc_n = _fix_invalid_escapes(self.CRASH_INPUT)
        assert esc == self.CRASH_INPUT
        assert esc_n == 0

    def test_oversized_line_skips_translation(self):
        src = "a" * _MAX_TOKENIZE_LINE_LENGTH + "\uff0c"
        code, n = _translate_outside_strings(src, _PUNCT_MAP)
        assert code == src
        assert n == 0

    def test_lone_surrogate_skips_translation(self):
        src = "x = \uff0c\ud800"
        code, n = _translate_outside_strings(src, _PUNCT_MAP)
        assert code == src
        assert n == 0

    def test_guard_does_not_block_normal_translation(self):
        code, n = _translate_outside_strings("print(1\uff0c2)", _PUNCT_MAP)
        assert code == "print(1,2)"
        assert n == 1


# --- _detect_missing_imports ---


class TestDetectMissingImports:
    def test_ruff_extracts_undefined_name_from_unicode_source(self):
        code = 'print("\u4f60\u597d")\nprint(missing_name)\n'
        assert _ruff_undefined_names(code) == {"missing_name"}

    @patch("subprocess.run")
    def test_ruff_undefined_names_uses_utf8_encoding(self, mock_run: MagicMock):
        mock_run.return_value = subprocess.CompletedProcess(
            args=["ruff"],
            returncode=1,
            stdout='[{"code":"F821","message":"Undefined name `missing_name`"}]',
            stderr="",
        )
        code = 'print("\u4f60\u597d")\nprint(missing_name)\n'

        assert _ruff_undefined_names(code) == {"missing_name"}
        assert mock_run.call_args.kwargs["encoding"] == "utf-8"

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
        code = 'import os\nimport os\nprint("\u4f60\u597d")\n'
        result = _ruff_fix(code)
        # ruff should process Unicode source without crashing.
        assert isinstance(result, str)
        assert "\u4f60\u597d" in result

    @patch("subprocess.run")
    def test_ruff_fix_uses_utf8_encoding(self, mock_run: MagicMock):
        code = 'print("\u4f60\u597d")\n'
        mock_run.return_value = subprocess.CompletedProcess(
            args=["ruff"], returncode=0, stdout=code, stderr=""
        )

        assert _ruff_fix(code) == code
        assert mock_run.call_args.kwargs["encoding"] == "utf-8"

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
        result = _ruff_format('message="\u4f60\u597d"\n')
        assert result == 'message = "\u4f60\u597d"\n'

    @patch("subprocess.run")
    def test_ruff_format_uses_utf8_encoding(self, mock_run: MagicMock):
        code = 'print("\u4f60\u597d")\n'
        mock_run.return_value = subprocess.CompletedProcess(
            args=["ruff"], returncode=0, stdout=code, stderr=""
        )

        assert _ruff_format(code) == code
        assert mock_run.call_args.kwargs["encoding"] == "utf-8"

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
    def test_undefined_names_unicode_encode_error_returns_empty(
        self, mock_run: MagicMock, loguru_records: list[dict[str, str]]
    ):
        code = 'print("\u4f60")\n'
        mock_run.side_effect = UnicodeEncodeError(
            "cp1252", "\u4f60", 0, 1, "character maps to <undefined>"
        )

        assert _ruff_undefined_names(code) == set()
        msgs = [r["message"] for r in loguru_records]
        assert any("UnicodeEncodeError" in m for m in msgs), msgs

    @patch("subprocess.run")
    def test_ruff_fix_unicode_encode_error_returns_original(
        self, mock_run: MagicMock, loguru_records: list[dict[str, str]]
    ):
        code = 'print("\u4f60")\n'
        mock_run.side_effect = UnicodeEncodeError(
            "cp1252", "\u4f60", 0, 1, "character maps to <undefined>"
        )

        assert _ruff_fix(code) == code
        msgs = [r["message"] for r in loguru_records]
        assert any("UnicodeEncodeError" in m for m in msgs), msgs

    @patch("subprocess.run")
    def test_ruff_format_unicode_encode_error_returns_original(
        self, mock_run: MagicMock, loguru_records: list[dict[str, str]]
    ):
        code = 'print("\u4f60")\n'
        mock_run.side_effect = UnicodeEncodeError(
            "cp1252", "\u4f60", 0, 1, "character maps to <undefined>"
        )

        assert _ruff_format(code) == code
        msgs = [r["message"] for r in loguru_records]
        assert any("UnicodeEncodeError" in m for m in msgs), msgs

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
        assert shutil.which("ruff") is None, "precondition: PATH cannot find ruff"
        assert _ruff_format("x=1\n") == "x = 1\n"
        assert "import json" in _detect_missing_imports("json.dumps({'a': 1})")


# ============================================================================
# _fix_invalid_escapes
# ============================================================================


# ============================================================================
# syntax_fix_before_exec integration
# ============================================================================


# ============================================================================
# _is_stdlib_module
# ============================================================================


# LLM repair helpers
# ============================================================================


# ============================================================================
# Deterministic fixes tests (syntax-fix v3)
# ============================================================================


def _is_broken(code: str) -> bool:
    """True when `code` fails to compile -- used to assert a fixture is a
    genuine syntax error before a fixer is run against it."""
    try:
        compile(code, "<t>", "exec")
    except SyntaxError:
        return True
    return False


# ============================================================================
# trace-v2 syntax_fix events (task #792 group B)
# ============================================================================
