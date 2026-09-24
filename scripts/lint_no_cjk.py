#!/usr/bin/env python3
"""Forbid raw CJK characters anywhere in the repo — English-only source (hard rule).

Run: `.venv/bin/python scripts/lint_no_cjk.py [path ...]` (defaults to the whole
repo, git-tracked files only; an explicit path that does not exist is an error
(stderr + exit 1) rather than a silent no-op). Also run automatically via
pre-commit and in CI (dedicated
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
- `shared/alerts_copy.py`, `shared/docs/pages_copy.py` — the Python locale
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
import sys
from pathlib import Path

# Project root (this script lives under scripts/)
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from scripts.structure import lint_common  # noqa: E402 - standalone script

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
_LOCALE_PY_FILES = frozenset({"shared/alerts_copy.py", "shared/docs/pages_copy.py"})


def _tracked_files() -> list[str]:
    """Every git-tracked file, repo-relative, posix separators."""
    return lint_common.tracked_files(_REPO_ROOT)


def _scan_file(rel_path: str) -> list[tuple[int, str, str]]:
    """Return violations [(lineno, char, line_stripped), ...]."""
    if _is_locale_path(rel_path) or rel_path in _LOCALE_PY_FILES:
        return []
    text = lint_common.read_utf8_text(_REPO_ROOT / rel_path)
    if text is None:
        return []
    violations: list[tuple[int, str, str]] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        m = _CJK_RE.search(line)
        if m is not None:
            violations.append((lineno, m.group(0), line.strip()))
    return violations


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if argv:
        scan, missing = lint_common.resolve_targets(argv, _REPO_ROOT)
        if missing:
            print(f"error: target path(s) not found: {', '.join(missing)}", file=sys.stderr)
            return 1
    else:
        scan = _tracked_files()

    total = 0
    for rel in scan:
        if rel.startswith(".git/"):
            continue
        for lineno, ch, content in _scan_file(rel):
            total += 1
            print(lint_common.format_violation(rel, lineno, f"U+{ord(ch):04X} {ch!r}", content))

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
