"""Forbid decorative emoji in core Python — keep agent / backend code glyph-free.

Run: `.venv/bin/python scripts/lint_no_emoji.py [path ...]` (defaults to scanning
the in-scope dirs below). Also run automatically via pre-commit hook.

## Why

Emoji creep into code from model output and copy-paste: a marker prefix, a log
line, a status string. In agent-facing / backend code they add nothing — they
cost tokens, render inconsistently across terminals, and read as noise. The
house rule is "no emoji unless explicitly requested"; this lint enforces it
where it matters and stays out of the way where emoji are a deliberate feature.

## Scope

Scans `.py` under the in-scope dirs (`_SCAN_DIRS`): the agent + backend layers.
Deliberate-UX surfaces are exempt and NOT scanned (`_EXEMPT_PREFIXES`):

- `cli/` — terminal output where emoji are intentional UX.
- `ava_builtins/skills/`, the doc axes (`decisions/`, `future/`, `conventions/`, `traces/`, `okf/`), `frontend/`, `migrations/` — prose / generated / content.
- `tests/cli/` — tests that mirror (and assert) the exempt surfaces.
## What counts as emoji

Pictographic / colored emoji (see `_EMOJI_RANGES`): the supplemental-symbol and
pictograph planes, misc symbols, dingbats, and regional-indicator flags. Plain
text marks that read as ASCII-art rather than decoration are allowed
(`_TEXT_MARK_ALLOW`: check / heavy-check / ballot-x / heavy-ballot-x), since
CLI-style pass/fail tables use them as text, not emoji.

## Exemption

When a line genuinely needs an emoji (e.g. a test asserting that a string is
emoji-free, which must name the character), add an inline `# emoji-ok: <reason>`
comment on that line. Kept inline + reasoned so each one is self-documenting at
the point of use.

Error format `file:line: U+XXXX 'c' | <line content>` + non-zero exit.
"""

from __future__ import annotations

import io
import sys
import tokenize
from pathlib import Path

# Project root (this script lives under scripts/)
_REPO_ROOT = Path(__file__).resolve().parent.parent

# In-scope dirs scanned by default (no argv) — agent + backend layers.
_SCAN_DIRS = (
    "agent",
    "ava",
    "gateway",
    "services",
    "shared",
    "plugins",
    "scripts",
    "mcps",
    "tests",
)

# Path prefixes (repo-relative, posix) that are never scanned — deliberate-UX
# surfaces and prose/content. Checked per file so a pre-commit-passed path under
# these trees is skipped no matter how the scan was invoked.
_EXEMPT_PREFIXES = (
    "cli/",
    "ui/",
    "ava_builtins/skills/",
    "decisions/",
    "future/",
    "conventions/",
    "traces/",
    "okf/",
    "assets/",
    "schedules/",
    "frontend/",
    "migrations/",
    "tests/cli/",
    "tests/ui/",
)

# Inline escape hatch: a line whose *comment* carries this token is not checked.
# Keep the reason inline (`# emoji-ok: asserts the marker is emoji-free`). Matched
# against real comment tokens only, so the token inside a string literal does not
# silently exempt the line.
_INLINE_EXEMPT = "# emoji-ok"

# Plain text marks allowed even though they fall in the dingbat range — CLI
# pass/fail tables use these as text: check (U+2713), heavy check (U+2714),
# ballot-x (U+2717), heavy ballot-x (U+2718).
_TEXT_MARK_ALLOW = frozenset({"✓", "✔", "✗", "✘"})

# Codepoint ranges read as pictographic / colored emoji. Deliberately starts at
# U+2600 so box-drawing (U+2500-257F, used in section-header comments) and
# general punctuation (em dash etc.) are untouched.
_EMOJI_RANGES: tuple[tuple[int, int], ...] = (
    (0x1F000, 0x1FAFF),  # supplemental symbols, pictographs, emoji, supplemental
    (0x2600, 0x26FF),  # miscellaneous symbols (warning sign, etc.)
    (0x2700, 0x27BF),  # dingbats (cross mark, sparkles, white check mark, etc.)
    (0x2B00, 0x2BFF),  # miscellaneous symbols and arrows
    (0x1F1E6, 0x1F1FF),  # regional indicator symbols (flag pairs)
)

# Emoji variation selector (VS16). The base char is what we flag; a lone VS16 is
# skipped so we do not double-report.
_VARIATION_SELECTOR = "️"


def _first_emoji(line: str) -> str | None:
    for i, ch in enumerate(line):
        nxt = line[i + 1] if i + 1 < len(line) else ""
        if ch in _TEXT_MARK_ALLOW:
            # Plain text mark, fine — unless a trailing VS16 forces it to render
            # as a colored emoji, in which case it is decorative after all.
            if nxt == _VARIATION_SELECTOR:
                return ch
            continue
        if ch == _VARIATION_SELECTOR:
            continue
        cp = ord(ch)
        if any(lo <= cp <= hi for lo, hi in _EMOJI_RANGES):
            return ch
    return None


def _exempt_comment_lines(text: str) -> set[int]:
    """Line numbers whose *comment* carries `# emoji-ok` (not the token in a string).

    Tokenizing is what distinguishes a real comment from the same characters
    appearing inside a string literal. Unparseable source (should not happen for
    committed code) falls back to a permissive per-line substring match.
    """
    try:
        return {
            tok.start[0]
            for tok in tokenize.generate_tokens(io.StringIO(text).readline)
            if tok.type == tokenize.COMMENT and _INLINE_EXEMPT in tok.string
        }
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return {i for i, line in enumerate(text.splitlines(), start=1) if _INLINE_EXEMPT in line}


def _is_exempt_path(rel_path: str) -> bool:
    return any(rel_path.startswith(pre) for pre in _EXEMPT_PREFIXES)


def _scan_file(path: Path, rel_path: str) -> list[tuple[int, str, str]]:
    """Return violations [(lineno, char, line_stripped), ...]."""
    if _is_exempt_path(rel_path):
        return []
    text = path.read_text(encoding="utf-8")
    exempt_lines = _exempt_comment_lines(text)
    violations: list[tuple[int, str, str]] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        if lineno in exempt_lines:
            continue
        ch = _first_emoji(line)
        if ch is not None:
            violations.append((lineno, ch, line.strip()))
    return violations


def _iter_py_files(roots: list[Path]) -> list[Path]:
    files: list[Path] = []
    for root in roots:
        if root.is_file() and root.suffix == ".py":
            files.append(root)
        elif root.is_dir():
            files.extend(root.rglob("*.py"))
    return files


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    # argv = explicit file list (manual run / pre-commit changed files); empty = scan every in-scope dir.
    targets = [Path(a).resolve() for a in argv] if argv else [_REPO_ROOT / d for d in _SCAN_DIRS]

    total = 0
    for path in sorted(_iter_py_files(targets)):
        if "__pycache__" in path.parts:
            continue
        try:
            rel = path.relative_to(_REPO_ROOT).as_posix()
        except ValueError:
            rel = path.as_posix()
        for lineno, ch, content in _scan_file(path, rel):
            total += 1
            print(f"{rel}:{lineno}: U+{ord(ch):04X} {ch!r} | {content}")

    if total:
        print(
            f"\n{total} emoji found in core code. Remove them, or if a line genuinely "
            f"needs the character add an inline `{_INLINE_EXEMPT}: <reason>`. See the "
            "docstring at the top of scripts/lint_no_emoji.py for scope and rationale.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
