"""
Steps 4-5 of the syntax-fix pipeline: ruff check --fix + ruff format.

Split out of plugin.py (2026-08-07, Task #1011).
"""

from __future__ import annotations

import subprocess

from ._imports import _RUFF_TIMEOUT_SECONDS, _log_ruff_give_up, _ruff_executable

# ---------------------------------------------------------------------------
# 4. ruff auto-fix
# ---------------------------------------------------------------------------


def _ruff_fix(code: str) -> str:
    """Run `ruff check --fix` via stdin; return fixed code.

    Auto-fixes: unused imports (F401), import ordering (I001),
    and other safe ruff rules. Diagnostics for unfixable issues
    go to stderr and are silently ignored.
    """
    try:
        proc = subprocess.run(  # noqa: S603
            [
                _ruff_executable(),
                "check",
                "--fix",
                "--stdin-filename",
                "script.py",
                "--quiet",
                "-",
            ],
            input=code,
            capture_output=True,
            text=True,
            timeout=_RUFF_TIMEOUT_SECONDS,
            check=False,
        )
        # Guard against ruff crashing / producing empty output
        if proc.returncode != 0 or not proc.stdout.strip():
            return code
        return proc.stdout
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        # FileNotFoundError: optional step skipped on a host without ruff —
        # one log line per process, then pass through. Timeout / OSError: a
        # repair step that gave up must be visible, not indistinguishable
        # from "ruff found nothing to fix" (issue #159) — warning with the
        # budget / input size / errno, then pass through unchanged (ruff is
        # off the deterministic-correctness path: check --fix only applies
        # safe auto-fixes, so leaving the source as-is is a style gap, not a
        # correctness one).
        _log_ruff_give_up("check --fix", code, exc)
        return code


def _ruff_format(code: str) -> str:
    """Run `ruff format` via stdin; return the formatted code.

    Style-only normalization (quotes, spacing, line wrapping, trailing
    commas) on top of `_ruff_fix`. Gated by settings.sandbox.syntax_fix_ruff_format.
    """
    try:
        proc = subprocess.run(  # noqa: S603
            [_ruff_executable(), "format", "--stdin-filename", "script.py", "-"],
            input=code,
            capture_output=True,
            text=True,
            timeout=_RUFF_TIMEOUT_SECONDS,
            check=False,
        )
        if proc.returncode != 0 or not proc.stdout.strip():
            return code
        return proc.stdout
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        # Same contract as _ruff_fix (issue #159): a timeout or OS error is a
        # visible warning; a missing ruff is logged once per process. Pass
        # through either way — format only restyles, never fixes an error.
        _log_ruff_give_up("format", code, exc)
        return code
