#!/usr/bin/env python3
"""Enforce a SKILL.md line-count ceiling to prevent progressive bloat.

Run: `.venv/bin/python scripts/lint_skill_md_size.py`.

## Why

SKILL.md files are loaded into agent context. A SKILL.md that grows too large
forces the agent to spend context budget on content it may not need for the
current task. The fix is the same as for AGENTS.md: progressive disclosure —
a root SKILL.md serves as an index, and sub-skills carry the depth.

## Thresholds

- Hard ceiling: 300 lines — fails the commit.
- Transitional zone: 250-300 lines — non-blocking note (reminder to split).

The ceiling applies per-file, not per-skill-directory. A skill directory with
sub-skills (each under 300 lines) is correctly structured.

## Exemption

Thresholds are repo-wide constants. If the ceiling genuinely needs to move,
bump the constant in the same PR and justify it in the commit message.
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent

_TRANSITIONAL_FLOOR = 250
_HARD_CEILING = 300

# Skill SKILL.md locations: repo-shipped + plugins + the repo's own git-tracked
# `.agents/skills/` (project-local skills, e.g. `.agents/skills/ava-sweeper/`;
# `.ava/skills` and `.claude/skills` link back to it). Every
# family is globbed at both the root and the sub-skill level — a sub-skill is
# where depth accumulates, so it is exactly where the ceiling has to reach.
_SKILL_MD_GLOBS = [
    "ava_builtins/skills/*/SKILL.md",
    "ava_builtins/skills/*/*/SKILL.md",
    "ava_builtins/plugins/*/skills/SKILL.md",
    "ava_builtins/plugins/*/skills/*/SKILL.md",
    ".agents/skills/*/SKILL.md",
    ".agents/skills/*/*/SKILL.md",
]


def _skill_md_files() -> list[Path]:
    files: list[Path] = []
    for pattern in _SKILL_MD_GLOBS:
        files.extend(_REPO_ROOT.glob(pattern))
    # Resolve links so a skill reachable through both `.agents/skills` and
    # `ava_builtins/skills/` (linked built-ins) is reported once, at its real
    # location.
    return sorted({p.resolve() for p in files})


def main(_argv: list[str] | None = None) -> int:
    exit_code = 0
    for path in _skill_md_files():
        rel = path.relative_to(_REPO_ROOT)
        text = path.read_text(encoding="utf-8")
        n_lines = len(text.splitlines())

        if n_lines > _HARD_CEILING:
            print(
                f"{rel}:{n_lines}: error: {n_lines} lines exceeds the "
                f"{_HARD_CEILING}-line hard ceiling — split into root + sub-skills "
                f"or move depth to reference files. If the ceiling genuinely needs "
                f"to move, bump _HARD_CEILING in scripts/lint_skill_md_size.py in "
                f"the same PR and justify it in the commit message.",
            )
            exit_code = 1
        elif n_lines > _TRANSITIONAL_FLOOR:
            print(
                f"{rel}:{n_lines}: note: {n_lines} lines is in the "
                f"{_TRANSITIONAL_FLOOR}-{_HARD_CEILING} transitional zone — "
                f"consider splitting before it hits the {_HARD_CEILING}-line "
                f"hard ceiling.",
            )

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
