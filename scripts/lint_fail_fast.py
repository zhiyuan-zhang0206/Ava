#!/usr/bin/env python3
"""Lint: ban the silent ``except ...: pass`` swallow.

AGENTS.md's "Fail fast" principle bans defensive exception handling that hides
real problems: catch an exception and do *literally nothing* and the program
keeps walking on corrupt state, the stack trace that would point at the source
is gone, and "new failure mode, forgot to handle it" bugs work silently for
weeks. This lint is the mechanical enforcement of that principle.

  Flagged: any exception handler whose body is exactly a lone ``pass`` ::

      try:
          risky()
      except Exception:
          pass            # <- flagged

      try:
          risky()
      except SomeError:    # multi-line form a grep for `except.*: pass`
          pass             #    would miss — caught here because we walk the AST

The check is AST-based (`ast.ExceptHandler` with `body == [ast.Pass]`), so both
the one-liner ``except X: pass`` and the multi-line form are caught, with zero
false positives — it only fires on "catch and do nothing", which is exactly the
swallow the principle forbids.

Sanctioned explicit form
------------------------
If you genuinely want to ignore a *specific* exception type, use
``contextlib.suppress(SpecificError)`` around the guarded call. It is
behaviorally identical to ``try/except SpecificError: pass`` but names the
intent at the call site, and this lint does **not** flag it::

      with contextlib.suppress(FileNotFoundError):
          path.unlink()

Inline exemption
----------------
For the rare best-effort path where suppressing a narrower type is not clean
(cancellation cleanup, a health probe that must never propagate), keep the
``pass`` and justify it with a ``# fail-fast-ok: <reason>`` comment on either
the ``except`` line or the ``pass`` line (mirrors ``# emoji-ok`` / ``# env-ok``
/ ``# noqa``)::

      except asyncio.CancelledError:
          pass  # fail-fast-ok: best-effort cancel cleanup, re-raise is wrong here

Scope: every ``*.py`` under the 7 framework dirs
``ava/ plugins/ agent/ gateway/ cli/ services/ shared/``. Run it standalone
(`.venv/bin/python scripts/lint_fail_fast.py`); fail -> exit 1. Also wired as the
`lint-fail-fast` pre-commit hook.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent

# The 7 framework dirs (same scope as the sweeper's fail-fast class).
_FRAMEWORK_DIRS = ("ava", "plugins", "agent", "gateway", "cli", "services", "shared")

_EXEMPT_MARKER = "# fail-fast-ok:"


def _is_lone_pass(handler: ast.ExceptHandler) -> bool:
    """True iff the handler body is exactly a single `pass` — the defensive
    swallow this lint bans. A handler that logs, re-raises, returns, or does any
    other work is fine."""
    return len(handler.body) == 1 and isinstance(handler.body[0], ast.Pass)


def violations_in_source(src: str, path: str) -> list[tuple[str, int, str]]:
    """Return the lone-`pass` exception-handler violations in `src`.

    Each violation is `(path, lineno, message)`, where `lineno` is the line of
    the `except` clause. A handler is exempt when either its `except` line or
    its `pass` line carries a `# fail-fast-ok:` marker — the AST has no comment
    nodes, so the marker is matched against the raw source lines.
    """
    tree = ast.parse(src, filename=path)
    lines = src.splitlines()
    out: list[tuple[str, int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler) or not _is_lone_pass(node):
            continue
        pass_lineno = node.body[0].lineno
        marked_lines = (node.lineno, pass_lineno)
        if any(_EXEMPT_MARKER in lines[ln - 1] for ln in marked_lines):
            continue
        out.append(
            (
                path,
                node.lineno,
                "except-handler body is a lone `pass` — a silent swallow AGENTS.md "
                "bans. Use `contextlib.suppress(SpecificError)` around the guarded "
                "call, or justify it with `# fail-fast-ok: <reason>` on the except "
                "or pass line.",
            )
        )
    return out


def _framework_py_files() -> list[Path]:
    files: list[Path] = []
    for d in _FRAMEWORK_DIRS:
        files += (_REPO_ROOT / d).rglob("*.py")
    return sorted(files)


def main() -> int:
    violations: list[tuple[str, int, str]] = []
    for path in _framework_py_files():
        rel = str(path.relative_to(_REPO_ROOT))
        src = path.read_text(encoding="utf-8")
        violations.extend(violations_in_source(src, rel))

    if violations:
        print("fail-fast lint failed (silent `except ...: pass` swallow):", file=sys.stderr)
        for rel, lineno, message in violations:
            print(f"  {rel}:{lineno}: {message}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
