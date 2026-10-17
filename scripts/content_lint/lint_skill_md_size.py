#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

from base.host.env.dotenv_boot import enter_scratch_home

if __name__ == "__main__":
    enter_scratch_home()

from base.packages.text_budget import LengthBudget, Level, Unit, evaluate

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# meiva:link scripts/content_lint/docs/content_lint.meiva.okf.md
_TRANSITIONAL_FLOOR = 250
_HARD_CEILING = 300

_SKILL_MD_GLOBS = [
    "meiva_builtins/skills/*/*/SKILL.md",
    "meiva_builtins/skills/*/*/*/SKILL.md",
    "meiva_builtins/plugins/*/skills/SKILL.md",
    "meiva_builtins/plugins/*/skills/*/SKILL.md",
    ".agents/skills/*/SKILL.md",
    ".agents/skills/*/*/SKILL.md",
]


# meiva:link scripts/content_lint/docs/content_lint.meiva.okf.md
def _skill_md_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for pattern in _SKILL_MD_GLOBS:
        files.extend(root.glob(pattern))
    return sorted({p.resolve() for p in files})


# meiva:link scripts/content_lint/docs/content_lint.meiva.okf.md
def main(_argv: list[str] | None = None, *, repo_root: Path | None = None) -> int:
    root = repo_root or _REPO_ROOT
    budget = LengthBudget(Unit.LINES, hard=_HARD_CEILING, soft=_TRANSITIONAL_FLOOR)
    exit_code = 0
    for path in _skill_md_files(root):
        rel = path.relative_to(root)
        verdict = evaluate(path.read_text(encoding="utf-8"), budget)
        n_lines = verdict.measured

        if verdict.level is Level.OVER:
            print(
                f"{rel}:{n_lines}: error: {n_lines} lines exceeds the "
                f"{_HARD_CEILING}-line hard ceiling — split into root + sub-skills "
                f"or move depth to reference files. If the ceiling genuinely needs "
                f"to move, bump _HARD_CEILING in scripts/content_lint/lint_skill_md_size.py in "
                f"the same PR and justify it in the commit message.",
            )
            exit_code = 1
        elif verdict.level is Level.SOFT:
            print(
                f"{rel}:{n_lines}: note: {n_lines} lines is in the "
                f"{_TRANSITIONAL_FLOOR}-{_HARD_CEILING} transitional zone — "
                f"consider splitting before it hits the {_HARD_CEILING}-line "
                f"hard ceiling.",
            )

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
