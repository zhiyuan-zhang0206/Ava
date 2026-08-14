"""Enforce an AGENTS.md line-count ceiling to prevent progressive bloat.

Run: `.venv/bin/python scripts/lint_agents_md_size.py`. Also run automatically via
pre-commit hook on any change to AGENTS.md.

## Why

AGENTS.md is the entry point injected into every agent's context. The community
best practice (OpenAI / Anthropic Agent Skills, AGENTS.md v1.1 proposal) converges
on a root AGENTS.md of <= 100 lines — light enough to stay in context permanently.

Ava's AGENTS.md was reduced from 385 to 81 lines (2026-07-14) by extracting
specialized rules into conventions/ following progressive disclosure. This lint
prevents it from creeping back up.

## Thresholds

- Hard ceiling: `_HARD_CEILING` lines — fails the commit.
- Transitional zone: `_TRANSITIONAL_FLOOR`..`_HARD_CEILING` — non-blocking note
  (reminder to trim). Both are the constants below; this prose does not restate
  their values, so a bump cannot leave the header lying.

## Exemption

The thresholds are repo-wide constants; there is no per-line or per-file exemption.
If the ceiling genuinely needs to move (e.g. a new always-active rule that cannot
live in the doc axes), bump the constant in the same PR — and justify it in the
commit message.
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_AGENTS_MD = _REPO_ROOT / "AGENTS.md"

# Line-count thresholds. The transitional zone prints a non-blocking note; the
# hard ceiling exits non-zero.
_TRANSITIONAL_FLOOR = 200
_HARD_CEILING = 245


def main(_argv: list[str] | None = None) -> int:
    if not _AGENTS_MD.exists():
        print(f"error: {_AGENTS_MD} not found", file=sys.stderr)
        return 1

    text = _AGENTS_MD.read_text(encoding="utf-8")
    n_lines = len(text.splitlines())

    exit_code = 0

    if n_lines > _HARD_CEILING:
        print(
            f"AGENTS.md:{n_lines}: error: {n_lines} lines exceeds the "
            f"{_HARD_CEILING}-line hard ceiling — trim or move content to "
            f"conventions/. If the ceiling genuinely needs to move, bump "
            f"_HARD_CEILING in scripts/lint_agents_md_size.py in the same PR "
            f"and justify it in the commit message.",
        )
        exit_code = 1
    elif n_lines > _TRANSITIONAL_FLOOR:
        print(
            f"AGENTS.md:{n_lines}: note: {n_lines} lines is in the "
            f"{_TRANSITIONAL_FLOOR}-{_HARD_CEILING} transitional zone — "
            f"consider trimming before it hits the {_HARD_CEILING}-line "
            f"hard ceiling.",
        )

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
