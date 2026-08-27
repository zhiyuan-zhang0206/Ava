#!/usr/bin/env python3
"""Forbid raw CJK characters anywhere in the repo — English-only source (hard rule).

Run: `.venv/bin/python scripts/lint_no_cjk.py` (whole repo, git-tracked files
only). Also run automatically via pre-commit and in CI (dedicated
`repo-language` job, so a docs-only or skills-only PR cannot slip raw CJK
past a job that classifies by code side).

## Why

User ruling 2026-08-27 (tightening the 2026-08-06 English-primary rule): the
repo must not contain Chinese characters at all — not "English primary", not
"Chinese only in skill descriptions". A raw CJK character anywhere in a
tracked file fails, with ONE exemption class: frontend i18n / locale copy —
the message catalogs and locale modules whose whole purpose is rendering
another language to the user.

## What is scanned

Every git-tracked file (`git ls-files`), so untracked build output
(node_modules, .next, coverage, logs) is out of scope by construction. Binary
files (NUL byte in the head, or non-UTF-8) are skipped. CJK means the East
Asian text ranges: CJK ideographs (incl. extension A and compatibility
ideographs), hiragana, katakana, hangul, CJK symbols/punctuation
(U+3000-U+303F), and fullwidth forms (U+FF00-U+FFEF) — the script set the
previous SKILL.md description gate used, plus the punctuation that real CJK
text always carries.

## Exemption — i18n / locale copy only

- `**/messages/*.json` — next-intl message catalogs (frontend locale data,
  e.g. `ui/web/messages/{en,zh}.json`).
- `**/locales/**` and `**/*.po` — gettext-style locale trees, should they
  ever appear.
- `shared/alerts_copy.py`, `shared/pages_copy.py` — the Python locale
  modules: IM alert-push copy and the page-expired page copy, both zh/en
  variants selected by `user_settings.display.language`, the same
  language-switching mechanism as the frontend catalogs (documented as
  locale modules in their own docstrings).

Nothing else is exempt — skill bodies, code comments, docs, tests, fixtures,
generated files (the codegen sources are what must be clean), demo apps all
fail on raw CJK. Functional CJK data (keyword lists, 2FA regexes, test
fixtures that must exercise CJK handling) is written as `\\uXXXX` escapes —
runtime-identical, and the repo stays ASCII-clean.

Error format `file:line: U+XXXX 'c' | <line content>` + non-zero exit.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

# Project root (this script lives under scripts/)
_REPO_ROOT = Path(__file__).resolve().parent.parent

# East Asian text: CJK ideographs U+3400-U+9FFF (incl. ext A) +
# compatibility ideographs U+F900-U+FAFF, hiragana U+3040-U+309F,
# katakana U+30A0-U+30FF, hangul U+AC00-U+D7A3, CJK symbols and
# punctuation U+3000-U+303F (ideographic space, full stops, brackets,
# etc.), and fullwidth forms U+FF00-U+FFEF. The script set matches the
# previous SKILL.md description-language gate; the punctuation/fullwidth
# ranges are included because real CJK text always carries them (the
# user ruling bans Chinese, and a fullwidth comma or corner bracket is
# Chinese too - ruff's RUF001 already treats them as ambiguous).
_CJK_RE = re.compile(
    "[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\u3040-\u309f\u30a0-\u30ff"
    "\uac00-\ud7a3\u3000-\u303f\uff00-\uffef]"
)

# Repo-relative path prefixes that are i18n / locale copy and never scanned.
# A path under these is locale DATA — the exact exemption the ruling grants.
_LOCALE_PATH_MARKERS = (
    "/messages/",  # next-intl catalogs: <dir>/messages/<lang>.json
    "/locales/",  # gettext-style locale trees
)
_LOCALE_SUFFIXES = (".po",)


def _is_locale_path(rel_path: str) -> bool:
    return rel_path.endswith(_LOCALE_SUFFIXES) or any(
        m in "/" + rel_path for m in _LOCALE_PATH_MARKERS
    )


# The Python locale modules (zh/en by user_settings display.language, the
# same language-switching mechanism as the frontend catalogs): the IM
# alert-push copy and the page-expired page copy.
_LOCALE_PY_FILES = frozenset({"shared/alerts_copy.py", "shared/pages_copy.py"})


def _tracked_files() -> list[str]:
    """Every git-tracked file, repo-relative, posix separators."""

    # Fixed command line ("git ls-files"), no untrusted input; the repo path
    # comes from this script's own __file__, never from callers.
    out = subprocess.run(  # noqa: S603 - fixed argv, repo-root derived from __file__
        ["git", "-C", str(_REPO_ROOT), "ls-files"],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    return [p for p in out.stdout.splitlines() if p]


def _scan_file(rel_path: str) -> list[tuple[int, str, str]]:
    """Return violations [(lineno, char, line_stripped), ...]."""
    if _is_locale_path(rel_path) or rel_path in _LOCALE_PY_FILES:
        return []
    path = _REPO_ROOT / rel_path
    try:
        data = path.read_bytes()
    except OSError:
        return []
    if b"\x00" in data[:8192]:
        return []  # binary
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return []  # not UTF-8 text (e.g. latin-1 legacy) — not CJK-relevant
    violations: list[tuple[int, str, str]] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        m = _CJK_RE.search(line)
        if m is not None:
            violations.append((lineno, m.group(0), line.strip()))
    return violations


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if argv:
        # Explicit paths: resolve against the repo root when a relative path
        # does not exist under the caller's CWD (pre-commit passes absolute
        # paths; a manual `python3 scripts/lint_no_cjk.py some/file` runs from
        # the repo root anyway, but a test or wrapper may not).
        targets = []
        for a in argv:
            p = Path(a)
            if not p.is_absolute():
                cand = _REPO_ROOT / p
                if cand.exists():
                    p = cand
            targets.append(p.resolve())
        files = []
        for t in targets:
            try:
                rel = t.relative_to(_REPO_ROOT).as_posix()
            except ValueError:
                rel = t.as_posix()
            if t.is_file():
                files.append(rel)
            elif t.is_dir():
                files.extend(
                    p.relative_to(_REPO_ROOT).as_posix() for p in t.rglob("*") if p.is_file()
                )
        # Explicit paths: scan them (git-tracked or not — a worktree edit
        # that is not yet added still must be caught).
        scan = sorted(set(files))
    else:
        scan = _tracked_files()

    total = 0
    for rel in scan:
        if rel.startswith(".git/"):
            continue
        for lineno, ch, content in _scan_file(rel):
            total += 1
            print(f"{rel}:{lineno}: U+{ord(ch):04X} {ch!r} | {content}")

    if total:
        print(
            "\nRaw CJK found in the repo. Translate prose/copy to English, or "
            "escape functional CJK data as \\uXXXX (runtime-identical). The only "
            "exemption is i18n / locale copy: <dir>/messages/*.json, */locales/*, "
            "*.po, and the shared/*_copy.py locale modules. See the docstring at the top of "
            "scripts/lint_no_cjk.py.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
