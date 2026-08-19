#!/usr/bin/env python3
"""Lint: every `path/to/file.py:symbol` anchor in the procedural docs must resolve.

The procedural docs cite code by anchor — `` `shared/trace.py:turn_span` `` — so a
reader can jump straight at the thing being described. Nothing validated them.
`lint_doc_symbols.py` looks like the guard but is not: its pattern is
`ava.<name>`, which validates the **SDK namespace** surface; a `file:symbol`
anchor falls entirely outside it. The one lint that ever resolved this shape
(`lint_trace_anchors.py`) covered `traces/` only and retired with that axis.

The drift class is a rename. The `session_span` -> `turn_span` rename invalidated
four references and all four were caught by hand, by three people, using three
methods — and the last one survived a grep aimed specifically at it. This lint
makes the next rename fail at the commit instead.

## What is checked

An anchor is `<path>.py:<symbol>` written **inside an inline-code span**. The
target file must exist, and `symbol` must be a name the file binds — `def` /
`async def` / `class` / assignment / import alias — resolved from the AST, never
from a text search, so a name that only appears in a comment or a string does not
count as resolving. Import aliases count because a re-export facade legitimately
exposes names it did not define, and an anchor into one is correct.

Only the FIRST segment is validated, mirroring `lint_doc_symbols.py`: an anchor
written `file.py:Class.method` checks `Class` and deliberately does not check
`method`. Attribute resolution is more surface than the drift class needs.

## Zero-false-positive design

  - **Code-span restriction.** Only anchors inside a `` `...` `` inline-code span
    are checked. A bare prose mention of a filename is never flagged.
  - **Shape.** `::` (a pytest node id, `tests/x.py::test_y`) does not match,
    because the character after the colon must start an identifier; and a line
    reference (`file.py:78`) does not match for the same reason. Neither is the
    dangling-symbol drift class. The path may carry a leading `.` or `./`, so
    `.agents/skills/.../x.py:main` resolves to the file the author actually wrote.

**Known miss, accepted deliberately:** an anchor written *inside a fenced code
block without backticks* is not seen — inside a fence, backticks are literal, so
the span rule finds nothing. The sibling lint carries fence state and scans whole
fenced lines; this one does not, because in a fence a bare `path.py:name` is far
more often command output or a grep result than a doc anchor, and flagging those
would cost the zero-false-positive property. Every anchor in the tree today is
backticked, so the miss is latent rather than live.

## Scope: the procedural axis, and not only Markdown

`conventions/` plus the repo's own dev skills under `.agents/skills/` — both tell
an agent what to do *right now*, so a stale anchor there is a live instruction to
open something that no longer exists. `decisions/` is a never-rewritten archive
and `future/` is design space that may name not-yet-built symbols; both stay out,
exactly as they are for `lint_doc_symbols.py` and `check_doc_references.py`.

Every **file** under those roots is scanned, not just `*.md`. A maintained
non-Markdown file carries live anchors too — `.agents/skills/ship-a-change/
reference/ci_watcher.py` cites `scripts/ci_utils.py:check_ci` in its module
docstring, and the `#65` rename hunt lost a reference in `db/schema.sql` to
exactly this `.md`-only assumption.

Symlinked directories ARE descended: 21 of the 38 entries under `.agents/skills/`
are symlinks to the built-in skills, so a `Path.rglob` walk (which does not follow
them) would silently skip most of that root.
"""

from __future__ import annotations

import ast
import os
import re
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DOCS_CONVENTIONS = _REPO_ROOT / "conventions"
_DEV_SKILLS = _REPO_ROOT / ".agents" / "skills"

# Inline-code span: text between single backticks, no embedded backtick or
# newline. Same span definition as `lint_doc_symbols.py`.
_INLINE_CODE = re.compile(r"`([^`\n]+)`")

# `<path>.py:<symbol>` — a slash-separated path ending in `.py`, optionally
# prefixed with `.` or `./`, then a colon, then a Python-identifier-shaped token.
#
# The lookbehind anchors the match to the start of a path token. Without it the
# engine happily starts mid-token: `.agents/skills/x.py` would match from `a`,
# capturing `agents/skills/x.py` and reporting a missing file the author never
# wrote. Requiring an identifier start after the colon is what excludes
# `tests/x.py::test_y` (pytest node id) and `file.py:78` (line reference).
_ANCHOR = re.compile(
    r"(?<![\w./-])((?:\.{1,2}/|\.)?[A-Za-z_0-9][A-Za-z_0-9./-]*\.py):([A-Za-z_][A-Za-z0-9_]*)"
)


def defined_names(source: str) -> set[str]:
    """Every name a `.py` source binds at any nesting depth.

    Covers `def` / `async def` / `class`, plain and annotated assignments, and
    import aliases. Walks the whole tree rather than the module body only, so an
    anchor naming a method or a nested helper still resolves: the guard is "this
    name is gone", which a rename triggers at any depth, and demanding module
    level would reject a legitimately documented method for nothing.
    """
    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, ast.Import | ast.ImportFrom):
            # A re-export facade binds names it did not define; an anchor into
            # one is correct and must not be flagged.
            names.update(a.asname or a.name.split(".")[0] for a in node.names)
    return names


def anchors_in(text: str) -> list[tuple[int, str, str]]:
    """`(lineno, path, symbol)` for every checkable anchor in `text`.

    "Checkable" = sits inside an inline-code span. Bare prose mentions are
    deliberately NOT returned — that is the zero-false-positive guard.
    """
    found: list[tuple[int, str, str]] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        for span in _INLINE_CODE.finditer(line):
            for m in _ANCHOR.finditer(span.group(1)):
                found.append((lineno, m.group(1), m.group(2)))
    return found


def iter_doc_files(root: Path) -> list[Path]:
    """Every file under `root`, descending symlinked directories.

    `Path.rglob` does not follow symlinked directories, which would skip 21 of
    the 38 entries under `.agents/skills/` (the built-in skills are linked in).
    Directories are visited once by resolved path so a link cycle cannot loop.
    """
    files: list[Path] = []
    seen_dirs: set[Path] = set()
    seen_files: set[Path] = set()
    for dirpath, dirnames, filenames in os.walk(root, followlinks=True):
        here = Path(dirpath).resolve()
        if here in seen_dirs:
            dirnames[:] = []
            continue
        seen_dirs.add(here)
        for name in filenames:
            path = Path(dirpath) / name
            resolved = path.resolve()
            if resolved not in seen_files:
                seen_files.add(resolved)
                files.append(path)
    return sorted(files)


def check() -> int:
    """Validate every procedural-doc anchor; return 0 (clean) or 1 (violations)."""
    violations: list[str] = []
    checked = 0
    docs = 0
    # Resolving the same target repeatedly is the common case (`runbook.md`
    # alone cites eight files), so parse each one once. `None` means unusable —
    # the reason is carried alongside so the message can say which.
    cache: dict[Path, set[str] | str] = {}

    for root in (_DOCS_CONVENTIONS, _DEV_SKILLS):
        if not root.is_dir():
            continue
        # Display paths relative to the repo root on the real run; fall back to
        # the root's parent when a test monkeypatches it to a tmp tree.
        display_root = _REPO_ROOT if root.is_relative_to(_REPO_ROOT) else root.parent
        for doc in iter_doc_files(root):
            text = doc.read_text(encoding="utf-8", errors="replace")
            found = anchors_in(text)
            if not found:
                continue
            docs += 1
            rel_doc = doc.relative_to(display_root)
            for lineno, path, symbol in found:
                checked += 1
                target = _REPO_ROOT / path
                if target not in cache:
                    cache[target] = _names_or_reason(target)
                names = cache[target]
                if isinstance(names, str):
                    violations.append(f"{rel_doc}:{lineno}: {path}:{symbol} — {names}")
                elif symbol not in names:
                    violations.append(
                        f"{rel_doc}:{lineno}: {path}:{symbol} — {path} binds no `{symbol}`"
                    )

    if violations:
        for v in violations:
            print(v, file=sys.stderr)
        print(
            f"\n{len(violations)} dangling file:symbol anchor(s) in conventions or "
            ".agents/skills. A rename or deletion left a doc pointing at code that is "
            "no longer there — update the anchor to the new name (or drop it).",
            file=sys.stderr,
        )
        return 1
    print(f"checked {checked} file:symbol anchor(s) across {docs} doc(s)")
    return 0


def _names_or_reason(target: Path) -> set[str] | str:
    """Names bound by `target`, or a string explaining why it could not be read.

    A target that does not exist, does not decode, or does not parse is reported
    as a violation naming the citing doc — rather than crashing the hook with a
    traceback that says nothing about which anchor caused it.
    """
    try:
        return defined_names(target.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return "no such file"
    except UnicodeDecodeError:
        return "file is not valid UTF-8"
    except SyntaxError as exc:
        return f"file does not parse ({exc.msg} at line {exc.lineno})"


def main() -> int:
    return check()


if __name__ == "__main__":
    sys.exit(main())
