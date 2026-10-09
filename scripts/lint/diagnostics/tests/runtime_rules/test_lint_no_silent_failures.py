"""`scripts/lint/no_silent_failures.py` — no broad handler swallows a failure unreported.

A broad `except` / `suppress(Exception)` that neither re-raises nor reports makes the
failure of its body indistinguishable from success. These cases pin what the lint flags
(each swallowing shape, including debug-only logging) and what it leaves alone: narrow
handlers, handlers that raise, log at WARNING+, emit, write to stderr or hand the
exception on, and the explicit reasoned marker.
"""

from __future__ import annotations

import importlib
import subprocess
import sys
from pathlib import Path

import pytest

_lint = importlib.import_module("scripts.lint.diagnostics.no_silent_failures")


def _lines(src: str) -> list[int]:
    return [lineno for lineno, _ in _lint.violations_in_source(src)]


@pytest.mark.parametrize(
    "src",
    [
        "import contextlib\nwith contextlib.suppress(Exception):\n    f()\n",
        "from contextlib import suppress\nwith suppress(BaseException):\n    f()\n",
        "from contextlib import suppress\nwith suppress(OSError, Exception):\n    f()\n",
        "import contextlib\nasync def g():\n    async with contextlib.suppress(Exception):\n        await f()\n",
        "try:\n    f()\nexcept Exception:\n    pass\n",
        "try:\n    f()\nexcept BaseException:\n    pass\n",
        "try:\n    f()\nexcept:\n    pass\n",
        "try:\n    f()\nexcept (OSError, Exception):\n    pass\n",
        "try:\n    f()\nexcept Exception:\n    x = None\n",
        "def g():\n    try:\n        return f()\n    except Exception:\n        return None\n",
        "for i in r:\n    try:\n        f(i)\n    except Exception:\n        continue\n",
        # Below WARNING is not a report: no production sink keeps it.
        'try:\n    f()\nexcept Exception as e:\n    logger.debug("x {}", e)\n',
        'try:\n    f()\nexcept Exception:\n    logger.opt(exception=True).info("x")\n',
        'try:\n    f()\nexcept Exception:\n    _log.info("x", exc_info=True)\n',
        # The handler's nested function runs later, not when the handler does.
        "try:\n    f()\nexcept Exception:\n    def later():\n        raise\n",
        "try:\n    f()\nexcept Exception:\n    cb = lambda: logger.warning('x')\n",
        # The marker needs a reason.
        "try:\n    f()\nexcept Exception:  # silent-ok:\n    pass\n",
    ],
)
def test_silent_broad_handler_is_flagged(src: str) -> None:
    assert _lines(src), src


@pytest.mark.parametrize(
    "src",
    [
        "import contextlib\nwith contextlib.suppress(FileNotFoundError):\n    f()\n",
        "from contextlib import suppress\nwith suppress(OSError, ValueError):\n    f()\n",
        "try:\n    f()\nexcept FileNotFoundError:\n    pass\n",
        "try:\n    f()\nexcept (OSError, ValueError):\n    pass\n",
        "try:\n    f()\nexcept Exception:\n    raise\n",
        "try:\n    f()\nexcept Exception as e:\n    raise RuntimeError('x') from e\n",
        "try:\n    f()\nexcept Exception:\n    cleanup()\n    raise\n",
        'try:\n    f()\nexcept Exception:\n    logger.warning("x")\n',
        'try:\n    f()\nexcept Exception:\n    logger.opt(exception=True).warning("x")\n',
        'try:\n    f()\nexcept Exception:\n    logger.error("x")\n',
        'try:\n    f()\nexcept Exception:\n    _log.exception("x")\n',
        'try:\n    f()\nexcept Exception:\n    logger.log("ERROR", "x")\n',
        'try:\n    f()\nexcept Exception:\n    emit("telemetry", "x")\n',
        "try:\n    f()\nexcept Exception:\n    telemetry.emit_prepared(e)\n",
        'import sys\ntry:\n    f()\nexcept Exception:\n    sys.stderr.write("x")\n',
        'import sys\ntry:\n    f()\nexcept Exception:\n    print("x", file=sys.stderr)\n',
        'import sys\ntry:\n    f()\nexcept Exception as e:\n    sys.exit(f"bad: {e}")\n',
        "try:\n    f()\nexcept Exception:\n    self.handleError(record)\n",
        # The exception is handed on: an error result, a failure list, a future.
        "try:\n    f()\nexcept Exception as e:\n    return {'error': str(e)}\n",
        "try:\n    f()\nexcept Exception as e:\n    failures.append(e)\n",
        "try:\n    f()\nexcept Exception as e:\n    fut.set_exception(e)\n",
        # The exception is handed on even when a debug line also mentions it.
        'try:\n    f()\nexcept Exception as e:\n    logger.debug("x {}", e)\n    return e\n',
        "try:\n    f()\nexcept Exception:  # silent-ok: the log sink's own failure path\n    pass\n",
        "from contextlib import suppress\n"
        "with suppress(Exception):  # silent-ok: the log sink's own failure path\n"
        "    f()\n",
    ],
)
def test_reported_or_narrow_handler_is_clean(src: str) -> None:
    assert _lines(src) == [], src


def test_reports_the_line_of_each_violation() -> None:
    src = (
        "import contextlib\n"
        "try:\n"
        "    f()\n"
        "except Exception:\n"
        "    pass\n"
        "with contextlib.suppress(Exception):\n"
        "    g()\n"
    )
    assert _lines(src) == [4, 6]


def test_nested_handlers_are_each_judged() -> None:
    src = (
        "try:\n"
        "    f()\n"
        "except Exception:\n"
        "    try:\n"
        "        g()\n"
        "    except Exception:\n"
        "        pass\n"
        "    raise\n"
    )
    # The outer handler re-raises; the inner one swallows.
    assert _lines(src) == [6]


def test_multiline_header_marker_is_honoured() -> None:
    src = (
        "try:\n"
        "    f()\n"
        "except (\n"
        "    Exception\n"
        "):  # silent-ok: reporting here would recurse into the sink\n"
        "    pass\n"
    )
    assert _lines(src) == []


def test_syntax_error_is_reported_not_skipped() -> None:
    assert _lines("def (:\n")


def test_default_scan_skips_tests_and_conftest() -> None:
    assert _lint._is_test_file("services/x/tests/test_a.py")
    assert _lint._is_test_file("services/x/test_a.py")
    assert _lint._is_test_file("services/x/conftest.py")
    assert not _lint._is_test_file("services/x/a.py")


def test_cli_exit_codes(tmp_path: Path) -> None:
    repo = Path(__file__).resolve().parents[5]
    bad = tmp_path / "bad.py"
    bad.write_text("try:\n    f()\nexcept Exception:\n    pass\n")
    good = tmp_path / "good.py"
    good.write_text("try:\n    f()\nexcept OSError:\n    pass\n")
    script = repo / "scripts" / "lint" / "diagnostics" / "no_silent_failures.py"

    failed = subprocess.run(  # noqa: S603 - fixed argv: this interpreter and the lint script
        [sys.executable, str(script), str(bad)], capture_output=True, text=True, check=False
    )
    assert failed.returncode == 1
    assert "bad.py:3:" in failed.stdout

    passed = subprocess.run(  # noqa: S603 - fixed argv: this interpreter and the lint script
        [sys.executable, str(script), str(good)], capture_output=True, text=True, check=False
    )
    assert passed.returncode == 0

    missing = subprocess.run(  # noqa: S603 - fixed argv: this interpreter and the lint script
        [sys.executable, str(script), str(tmp_path / "nope.py")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert missing.returncode == 1
    assert "not found" in missing.stderr
