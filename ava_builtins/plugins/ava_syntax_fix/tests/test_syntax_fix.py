import pathlib
import shutil
import subprocess
import sys
from unittest.mock import MagicMock, patch

import pytest

from ava_builtins.plugins.ava_syntax_fix._imports import (
    ruff_undefined_names,
)
from ava_builtins.plugins.ava_syntax_fix._punct import (
    FULLWIDTH_QUOTE_MAP,
    MAX_TOKENIZE_LINE_LENGTH,
    PUNCT_MAP,
    translate_outside_strings,
)
from ava_builtins.plugins.ava_syntax_fix.agent_runtime import (
    detect_missing_imports,
    fix_chinese_punctuation,
    insert_imports,
    ruff_executable,
    ruff_fix,
    ruff_format,
)


class TestFixChinesePunctuation:
    def test_no_chinese_no_change(self):
        code, n = fix_chinese_punctuation('print("hello")')
        assert code == 'print("hello")'
        assert n == 0

    def test_chinese_comma_to_ascii(self):
        code, n = fix_chinese_punctuation("print(1\uff0c2)")
        assert code == "print(1,2)"
        assert n == 1

    def test_chinese_period_to_dot(self):
        code, n = fix_chinese_punctuation("import os\u3002")
        assert code == "import os."
        assert n == 1

    def test_chinese_left_quote_to_ascii(self):
        code, n = fix_chinese_punctuation("print\uff081\uff09")
        assert code == "print(1)"
        assert n == 2

    def test_multiple_replacements(self):
        code, n = fix_chinese_punctuation("a\uff0cb\u3002c\uff08d\uff09")
        assert code == "a,b.c(d)"
        assert n == 4

    def test_empty_code(self):
        code, n = fix_chinese_punctuation("")
        assert code == ""
        assert n == 0

    def test_in_string_punctuation_preserved(self):
        src = 'msg = "\u4f60\u597d\uff0c\u4e16\u754c"'
        code, n = fix_chinese_punctuation(src)
        assert code == src
        assert n == 0

    def test_in_triple_quote_preserved(self):
        src = 'p = """\u95ee\u9898\u80cc\u666f\uff1a\u8bf7\u7528 \u201c\u667a\u80fd\u201d \u6a21\u5f0f\u3002"""'
        code, n = fix_chinese_punctuation(src)
        assert code == src
        assert n == 0
        compile(code, "<t>", "exec")

    def test_comment_punctuation_preserved(self):
        src = "x = 1  # \u8c03\u7528\uff08\u91cd\u8981\uff09"
        code, n = fix_chinese_punctuation(src)
        assert code == src
        assert n == 0

    def test_code_position_fixed_but_string_preserved(self):
        src = 'print\uff08"a\uff0cb"\uff09'
        code, n = fix_chinese_punctuation(src)
        assert code == 'print("a\uff0cb")'
        assert n == 2

    def test_fullwidth_quote_delimiters_fixed_interior_preserved(self):
        src = "msg = \u201c\u4f60\u597d\uff0c\u4e16\u754c\u201d"
        code, n = fix_chinese_punctuation(src)
        assert code == 'msg = "\u4f60\u597d\uff0c\u4e16\u754c"'
        assert n == 2
        compile(code, "<t>", "exec")

    def test_tokenize_failure_returns_unchanged(self):
        src = 'x = ("a\uff0cb"'
        code, n = fix_chinese_punctuation(src)
        assert code == src
        assert n == 0


class TestTranslateOutsideStringsCrashGuard:
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
        code, n = translate_outside_strings(self.CRASH_INPUT, FULLWIDTH_QUOTE_MAP)
        assert code == self.CRASH_INPUT
        assert n == 0

    @pytest.mark.skipif(
        sys.version_info < (3, 12),
        reason="the guarded C-tokenizer crash exists on 3.12+ (SystemError on "
        "3.12, MemoryError on 3.13+; the guard degrades both)",
    )
    def test_crash_input_degrades_to_noop_punct_pass(self):
        code, n = translate_outside_strings(self.CRASH_INPUT, PUNCT_MAP)
        assert code == self.CRASH_INPUT
        assert n == 0

    @pytest.mark.skipif(
        sys.version_info < (3, 12),
        reason="3.11 tokenizes the input (and would translate the fullwidth "
        "comma); the guard degrades the 3.12+ crash manifestations",
    )
    def test_pipeline_degrades_on_crash_input(self):
        src = self.CRASH_INPUT.replace("() => {", "() => {\uff0c")
        code, n = fix_chinese_punctuation(src)
        assert code == src
        assert n == 0

    @pytest.mark.skipif(
        sys.version_info < (3, 12),
        reason="3.11 tokenizes the input (and would translate the fullwidth "
        "comma); the guard degrades the 3.12+ crash manifestations",
    )
    def test_escapes_fixer_degrades_on_crash_input(self):
        from ava_builtins.plugins.ava_syntax_fix._escapes import fix_invalid_escapes

        code, n = fix_invalid_escapes(self.CRASH_INPUT)
        assert code == self.CRASH_INPUT
        assert n == 0

    def test_memoryerror_from_tokenizer_degrades_to_noop(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import tokenize

        def boom(_readline: object) -> object:
            raise MemoryError("simulated C-tokenizer MemoryError")

        monkeypatch.setattr(tokenize, "generate_tokens", boom)
        code, n = translate_outside_strings(self.CRASH_INPUT, PUNCT_MAP)
        assert code == self.CRASH_INPUT
        assert n == 0

        from ava_builtins.plugins.ava_syntax_fix._escapes import fix_invalid_escapes

        esc, esc_n = fix_invalid_escapes(self.CRASH_INPUT)
        assert esc == self.CRASH_INPUT
        assert esc_n == 0

    def test_oversized_line_skips_translation(self):
        src = "a" * MAX_TOKENIZE_LINE_LENGTH + "\uff0c"
        code, n = translate_outside_strings(src, PUNCT_MAP)
        assert code == src
        assert n == 0

    def test_lone_surrogate_skips_translation(self):
        src = "x = \uff0c\ud800"
        code, n = translate_outside_strings(src, PUNCT_MAP)
        assert code == src
        assert n == 0

    def test_guard_does_not_block_normal_translation(self):
        code, n = translate_outside_strings("print(1\uff0c2)", PUNCT_MAP)
        assert code == "print(1,2)"
        assert n == 1


class TestDetectMissingImports:
    def test_ruff_extracts_undefined_name_from_unicode_source(self):
        code = 'print("\u4f60\u597d")\nprint(missing_name)\n'
        assert ruff_undefined_names(code) == {"missing_name"}

    @patch("subprocess.run")
    def test_ruff_undefined_names_uses_utf8_encoding(self, mock_run: MagicMock):
        mock_run.return_value = subprocess.CompletedProcess(
            args=["ruff"],
            returncode=1,
            stdout='[{"code":"F821","message":"Undefined name `missing_name`"}]',
            stderr="",
        )
        code = 'print("\u4f60\u597d")\nprint(missing_name)\n'

        assert ruff_undefined_names(code) == {"missing_name"}
        assert mock_run.call_args.kwargs["encoding"] == "utf-8"

    def test_empty_code_returns_empty(self):
        assert detect_missing_imports("") == []

    def test_whitespace_only_returns_empty(self):
        assert detect_missing_imports("   \n  ") == []

    def test_no_unknown_usage_returns_empty(self):
        assert detect_missing_imports("x = 1") == []

    def test_detects_json_usage(self):
        result = detect_missing_imports("json.dumps({'a': 1})")
        assert "import json" in result

    def test_detects_math_usage(self):
        result = detect_missing_imports("math.sqrt(4)")
        assert "import math" in result

    def test_detects_re_usage(self):
        result = detect_missing_imports("re.compile(r'.*')")
        assert "import re" in result

    def test_detects_datetime_usage(self):
        result = detect_missing_imports("datetime.datetime.now()")
        assert "import datetime" in result

    def test_skips_already_imported(self):
        code = "import json\njson.dumps({'a': 1})"
        assert detect_missing_imports(code) == []

    def test_skips_already_from_imported(self):
        code = "from json import dumps\ndumps({'a': 1})"
        assert detect_missing_imports(code) == []

    def test_skips_builtins(self):
        assert detect_missing_imports("True") == []
        assert detect_missing_imports("print('hello')") == []
        assert detect_missing_imports("len([1,2])") == []

    def test_detects_os_import(self):
        result = detect_missing_imports("os.getcwd()")
        assert "import os" in result

    def test_detects_sys_import(self):
        result = detect_missing_imports("sys.argv")
        assert "import sys" in result

    def test_detects_time_import(self):
        result = detect_missing_imports("time.sleep(1)")
        assert "import time" in result

    def test_detects_dotted_import_urllib(self):
        result = detect_missing_imports("urllib.parse.urlparse('http://x.com')")
        assert "import urllib.parse" in result

    def test_detects_concurrent_futures(self):
        result = detect_missing_imports("concurrent.futures.ThreadPoolExecutor()")
        assert any("concurrent.futures" in s for s in result)

    def test_multiple_missing_sorted(self):
        code = """
json.dumps(x)
math.sqrt(y)
re.compile(z)
"""
        result = detect_missing_imports(code)
        imports = [s for s in result if s.startswith("import ")]
        assert imports == sorted(imports)

    def test_ava_transitive_skipped(self):
        code = "os.getcwd()\nsys.argv\ntime.time()"
        result = detect_missing_imports(code)
        stmts = set(result)
        assert "import os" in stmts
        assert "import sys" in stmts
        assert "import time" in stmts

    def test_ava_detected(self):
        result = detect_missing_imports("ava.files.read('x')")
        assert "import ava" in result

    def test_ava_already_imported_skipped(self):
        code = "import ava\nava.files.read('x')"
        assert detect_missing_imports(code) == []

    def test_import_below_first_usage_counts_as_missing(self):
        code = "os.environ.get('X')\nimport os\nos.walk('/')\n"
        result = detect_missing_imports(code)
        assert "import os" in result

    def test_import_above_first_usage_skipped(self):
        code = "import os\nos.environ.get('X')\n"
        assert detect_missing_imports(code) == []

    def test_dotted_import_below_first_usage_counts_as_missing(self):
        code = "urllib.parse.urlparse('http://x')\nimport urllib.parse\n"
        assert "import urllib.parse" in detect_missing_imports(code)

    def test_from_import_below_first_usage_counts_as_missing(self):
        code = "json.dumps({'a': 1})\nfrom json import dumps\n"
        assert "import json" in detect_missing_imports(code)


class TestInsertImports:
    def test_simple_code_inserts_at_top(self):
        code = "print('hello')"
        result = insert_imports(code, ["import json"])
        assert result.startswith("import json\n")
        assert "print('hello')" in result

    def test_after_shebang(self):
        code = "#!/usr/bin/env python\nprint('hello')"
        result = insert_imports(code, ["import json"])
        lines = result.split("\n")
        assert lines[0] == "#!/usr/bin/env python"
        assert "import json" in lines[1]

    def test_after_module_docstring(self):
        code = '"""Module docstring.\n"""\nprint(1)'
        result = insert_imports(code, ["import json"])
        lines = result.split("\n")
        assert lines[0] == '"""Module docstring.'
        assert "import json" in result
        assert "print(1)" in result

    def test_after_comments(self):
        code = "# comment 1\n# comment 2\nprint(1)"
        result = insert_imports(code, ["import json"])
        lines = result.split("\n")
        assert lines[0] == "# comment 1"
        assert lines[1] == "# comment 2"
        assert "import json" in lines[2]

    def test_empty_imports_no_change(self):
        code = "print(1)"
        result = insert_imports(code, [])
        assert "print(1)" in result

    def test_multiple_imports(self):
        code = "print(1)"
        result = insert_imports(code, ["import json", "import math"])
        assert "import json" in result
        assert "import math" in result


class TestRuffFix:
    def test_ruff_available_runs_fix(self):
        code = 'import os\nimport os\nprint("\u4f60\u597d")\n'
        result = ruff_fix(code)
        assert isinstance(result, str)
        assert "\u4f60\u597d" in result

    @patch("subprocess.run")
    def test_ruff_fix_uses_utf8_encoding(self, mock_run: MagicMock):
        code = 'print("\u4f60\u597d")\n'
        mock_run.return_value = subprocess.CompletedProcess(
            args=["ruff"], returncode=0, stdout=code, stderr=""
        )

        assert ruff_fix(code) == code
        assert mock_run.call_args.kwargs["encoding"] == "utf-8"

    @patch("subprocess.run")
    def test_ruff_not_found_returns_original(self, mock_run):
        mock_run.side_effect = FileNotFoundError
        code = "import os\nimport os\n"
        assert ruff_fix(code) == code

    @patch("subprocess.run")
    def test_ruff_timeout_returns_original(self, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired("ruff", 5)
        code = "import os\n"
        assert ruff_fix(code) == code

    @patch("subprocess.run")
    def test_ruff_nonzero_returns_original(self, mock_run):
        mock_run.return_value = subprocess.CompletedProcess(
            args=["ruff"], returncode=1, stdout="", stderr="error"
        )
        code = "import os\n"
        assert ruff_fix(code) == code

    @patch("subprocess.run")
    def test_ruff_empty_stdout_returns_original(self, mock_run):
        mock_run.return_value = subprocess.CompletedProcess(
            args=["ruff"], returncode=0, stdout="", stderr=""
        )
        code = "import os\n"
        assert ruff_fix(code) == code


class TestRuffFormat:
    def test_ruff_available_normalizes_style(self):
        result = ruff_format('message="\u4f60\u597d"\n')
        assert result == 'message = "\u4f60\u597d"\n'

    @patch("subprocess.run")
    def test_ruff_format_uses_utf8_encoding(self, mock_run: MagicMock):
        code = 'print("\u4f60\u597d")\n'
        mock_run.return_value = subprocess.CompletedProcess(
            args=["ruff"], returncode=0, stdout=code, stderr=""
        )

        assert ruff_format(code) == code
        assert mock_run.call_args.kwargs["encoding"] == "utf-8"

    @patch("subprocess.run")
    def test_ruff_not_found_returns_original(self, mock_run):
        mock_run.side_effect = FileNotFoundError
        assert ruff_format("x=1\n") == "x=1\n"

    @patch("subprocess.run")
    def test_ruff_timeout_returns_original(self, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired("ruff", 5)
        assert ruff_format("x=1\n") == "x=1\n"

    @patch("subprocess.run")
    def test_ruff_nonzero_returns_original(self, mock_run):
        mock_run.return_value = subprocess.CompletedProcess(
            args=["ruff"], returncode=2, stdout="", stderr="error"
        )
        assert ruff_format("x=1\n") == "x=1\n"

    @patch("subprocess.run")
    def test_ruff_empty_stdout_returns_original(self, mock_run):
        mock_run.return_value = subprocess.CompletedProcess(
            args=["ruff"], returncode=0, stdout="", stderr=""
        )
        assert ruff_format("x=1\n") == "x=1\n"


class TestRuffGiveUpLogging:
    @patch("subprocess.run")
    def test_undefined_names_unicode_encode_error_returns_empty(
        self, mock_run: MagicMock, loguru_records: list[dict[str, str]]
    ):
        code = 'print("\u4f60")\n'
        mock_run.side_effect = UnicodeEncodeError(
            "cp1252", "\u4f60", 0, 1, "character maps to <undefined>"
        )

        assert ruff_undefined_names(code) == set()
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

        assert ruff_fix(code) == code
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

        assert ruff_format(code) == code
        msgs = [r["message"] for r in loguru_records]
        assert any("UnicodeEncodeError" in m for m in msgs), msgs

    @patch("subprocess.run")
    def test_ruff_fix_timeout_logs_warning(self, mock_run, loguru_records):
        mock_run.side_effect = subprocess.TimeoutExpired("ruff", 5)
        code = "import os\n"
        assert ruff_fix(code) == code
        msgs = [r["message"] for r in loguru_records]
        assert any("did not finish within 5s" in m and "char source" in m for m in msgs), msgs

    @patch("subprocess.run")
    def test_ruff_fix_oserror_logs_errno(self, mock_run, loguru_records):
        mock_run.side_effect = OSError(24, "Too many open files")
        code = "import os\n"
        assert ruff_fix(code) == code
        msgs = [r["message"] for r in loguru_records]
        assert any("errno=24" in m and "Too many open files" in m for m in msgs), msgs

    @patch("subprocess.run")
    def test_ruff_format_timeout_logs_warning(self, mock_run, loguru_records):
        mock_run.side_effect = subprocess.TimeoutExpired("ruff", 5)
        code = "x=1\n"
        assert ruff_format(code) == code
        msgs = [r["message"] for r in loguru_records]
        assert any("did not finish within 5s" in m and "char source" in m for m in msgs), msgs

    @patch("subprocess.run")
    def test_ruff_missing_reports_each_skipped_stage(self, mock_run, loguru_records):
        mock_run.side_effect = FileNotFoundError
        for code in ("a = 1\n", "b = 2\n"):
            assert ruff_fix(code) == code
        assert ruff_format("c = 3\n") == "c = 3\n"
        msgs = [r["message"] for r in loguru_records]
        assert sum("not found" in m for m in msgs) == 3, msgs

    @patch("subprocess.run")
    def test_undefined_names_timeout_logs_warning(self, mock_run, loguru_records):
        mock_run.side_effect = subprocess.TimeoutExpired("ruff", 5)
        assert ruff_undefined_names("import os\n") == set()
        msgs = [r["message"] for r in loguru_records]
        assert any("did not finish within 5s" in m and "check --select F821" in m for m in msgs), (
            msgs
        )

    @patch("subprocess.run")
    def test_undefined_names_oserror_logs_errno(self, mock_run, loguru_records):
        mock_run.side_effect = OSError(28, "No space left on device")
        assert ruff_undefined_names("import os\n") == set()
        msgs = [r["message"] for r in loguru_records]
        assert any("errno=28" in m for m in msgs), msgs


class TestRuffExecutableResolution:
    def test_resolves_to_the_interpreters_own_ruff(self):
        resolved = pathlib.Path(ruff_executable())
        assert resolved.is_file()
        assert resolved.parent == pathlib.Path(sys.executable).parent

    def test_ruff_backed_fixers_work_with_ruff_absent_from_path(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv("PATH", "")
        assert shutil.which("ruff") is None, "precondition: PATH cannot find ruff"
        assert ruff_format("x=1\n") == "x = 1\n"
        assert "import json" in detect_missing_imports("json.dumps({'a': 1})")


def is_broken(code: str) -> bool:
    try:
        compile(code, "<t>", "exec")
    except SyntaxError:
        return True
    return False
