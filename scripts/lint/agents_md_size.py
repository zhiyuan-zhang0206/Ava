from __future__ import annotations

import sys
from pathlib import Path

from base.host.env.dotenv_boot import enter_scratch_home

if __name__ == "__main__":
    enter_scratch_home()

from base.packages.text_budget import LengthBudget, Level, Unit, evaluate

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# ava:ref scripts/lint/docs/agents-md-size.ava.okf.md#agents-md-size-ceiling
_TRANSITIONAL_FLOOR = 200
_HARD_CEILING = 245


# ava:ref scripts/lint/docs/agents-md-size.ava.okf.md#agents-md-size-ceiling
def main(_argv: list[str] | None = None, *, repo_root: Path | None = None) -> int:
    agents_md = (repo_root or _REPO_ROOT) / "AGENTS.md"
    if not agents_md.exists():
        print(f"error: {agents_md} not found", file=sys.stderr)
        return 1

    verdict = evaluate(
        agents_md.read_text(encoding="utf-8"),
        LengthBudget(Unit.LINES, hard=_HARD_CEILING, soft=_TRANSITIONAL_FLOOR),
    )
    n_lines = verdict.measured

    if verdict.level is Level.OVER:
        print(
            f"AGENTS.md:{n_lines}: error: {n_lines} lines exceeds the "
            f"{_HARD_CEILING}-line hard ceiling — trim or move content to "
            f"docs/conventions/. If the ceiling genuinely needs to move, bump "
            f"_HARD_CEILING in scripts/lint/agents_md_size.py in the same PR "
            f"and justify it in the commit message.",
        )
        return 1
    if verdict.level is Level.SOFT:
        print(
            f"AGENTS.md:{n_lines}: note: {n_lines} lines is in the "
            f"{_TRANSITIONAL_FLOOR}-{_HARD_CEILING} transitional zone — "
            f"consider trimming before it hits the {_HARD_CEILING}-line "
            f"hard ceiling.",
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
