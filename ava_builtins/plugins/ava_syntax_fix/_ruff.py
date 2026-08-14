"""
Steps 4-5 of the syntax-fix pipeline: ruff check --fix + ruff format.

Split out of plugin.py (2026-08-07, Task #1011).
"""

from __future__ import annotations

import subprocess

from ._imports import _ruff_executable

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
            timeout=5,
            check=False,
        )
        # Guard against ruff crashing / producing empty output
        if proc.returncode != 0 or not proc.stdout.strip():
            return code
        return proc.stdout
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return code  # ruff not available or slow; pass through unchanged


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
            timeout=5,
            check=False,
        )
        if proc.returncode != 0 or not proc.stdout.strip():
            return code
        return proc.stdout
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return code  # ruff not available or slow; pass through unchanged
