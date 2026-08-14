"""Tests for agent/graph/_agent_traceback.py.

The agent-facing render must keep only the agent's own ``<agent_code>`` frames and
never leak the exec harness, SDK, plugin, or stdlib frames. The full render is the
unfiltered counterpart for the server logs. Exceptions are produced by genuinely
compiling/exec'ing source under the ``<agent_code>`` filename so the frame
filenames match what `_exec.py` produces at runtime; this test module's own frame
(the `exec` call site) stands in for the exec harness frame and must be dropped.
"""

from __future__ import annotations

import linecache

import pytest

from agent.graph._agent_traceback import (
    AGENT_CODE_FILENAME,
    format_agent_traceback,
    format_full_traceback,
    register_agent_source,
)

_THIS_FILE_MARKER = "test_agent_traceback"


def _raise_in_agent_code(code: str) -> BaseException:
    """Compile + exec `code` under ``<agent_code>`` and return the exception it
    raised. Does not register the source — tests that assert on source-line
    rendering call `register_agent_source` themselves."""
    try:
        exec(compile(code, AGENT_CODE_FILENAME, "exec"), {"__name__": "__agent_code__"})
    except Exception as e:
        return e
    raise AssertionError("snippet did not raise")


@pytest.fixture(autouse=True)
def _clear_agent_linecache():
    """Drop any stale ``<agent_code>`` linecache entry so source-line presence is
    controlled per-test, not leaked between tests."""
    linecache.cache.pop(AGENT_CODE_FILENAME, None)
    yield
    linecache.cache.pop(AGENT_CODE_FILENAME, None)


def test_pure_agent_error_keeps_only_agent_frame_no_marker():
    exc = _raise_in_agent_code("raise ValueError('boom')")
    out = format_agent_traceback(exc)

    assert 'File "<agent_code>", line 1' in out
    assert "ValueError: boom" in out
    # No harness frame (this test file's exec call site).
    assert _THIS_FILE_MARKER not in out
    # Raise is in the agent's own code, nothing below it — no collapse marker.
    assert "..." not in out


def test_error_from_library_call_drops_lib_frames_no_marker():
    exc = _raise_in_agent_code("import json\njson.loads('{')")
    out = format_agent_traceback(exc)

    assert 'File "<agent_code>"' in out
    assert "JSONDecodeError" in out
    # The stdlib json frames must not leak.
    assert "decoder.py" not in out
    assert "json/__init__.py" not in out and "json\\__init__.py" not in out
    assert _THIS_FILE_MARKER not in out
    # Collapse marker is no longer emitted — the agent's own frame plus the
    # exception type and message are always sufficient for debugging.
    assert "raised in library" not in out
    assert "framework" not in out


def test_full_traceback_is_unfiltered():
    exc = _raise_in_agent_code("import json\njson.loads('{')")
    full = format_full_traceback(exc)

    # The full render is for the logs: it keeps the frames the agent view hides.
    assert "decoder.py" in full
    assert "<agent_code>" in full


def test_chained_exception_filters_every_level():
    code = "\n".join(
        [
            "try:",
            "    1 / 0",
            "except ZeroDivisionError as e:",
            "    raise ValueError('wrapped') from e",
        ]
    )
    exc = _raise_in_agent_code(code)
    out = format_agent_traceback(exc)

    assert "ZeroDivisionError" in out
    assert "ValueError: wrapped" in out
    assert "The above exception was the direct cause of the following exception:" in out
    # Both levels are agent code; no harness/library frame leaks at either level.
    assert _THIS_FILE_MARKER not in out


def test_syntax_error_shows_agent_location_not_harness():
    exc = _raise_in_agent_code("def f(:\n    pass\n")
    out = format_agent_traceback(exc)

    assert 'File "<agent_code>"' in out
    assert "SyntaxError" in out
    # The compile() call site (this file) must not appear.
    assert _THIS_FILE_MARKER not in out


def test_register_agent_source_makes_offending_line_visible():
    code = "raise ValueError('needle-12345')"
    exc = _raise_in_agent_code(code)

    # Without a linecache entry the agent frame has no source line.
    assert "raise ValueError('needle-12345')" not in format_agent_traceback(exc)

    register_agent_source(code)
    out = format_agent_traceback(exc)
    assert "raise ValueError('needle-12345')" in out


def test_caret_annotation_lines_are_stripped():
    code = "x = 1 / 0"
    register_agent_source(code)
    exc = _raise_in_agent_code(code)
    out = format_agent_traceback(exc)

    # The source line must be present.
    assert "x = 1 / 0" in out
    assert "ZeroDivisionError" in out
    # But the caret annotation under it must not.
    # The caret line consists of only whitespace, ^, and ~.
    assert "~^~" not in out
    assert "^^^" not in out


def test_no_agent_frame_renders_exception_only():
    """An exception whose traceback has no ``<agent_code>`` frame at all renders
    just the exception type and message — no collapse marker, no empty stack."""
    # Build an exception caught entirely outside agent code (no <agent_code> frame).
    try:
        {}["missing"]
    except KeyError as e:
        exc = e
    else:
        raise AssertionError("expected KeyError")

    out = format_agent_traceback(exc)
    assert "KeyError" in out
    assert "Traceback" not in out
    # No marker — the exception alone is sufficient.
    assert "framework" not in out
