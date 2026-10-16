#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

from base.host.env.dotenv_boot import enter_scratch_home

if __name__ == "__main__":
    enter_scratch_home()

from base.packages.skills.index import SkillFile, SkillIndex
from base.packages.skills.names import match_key
from base.packages.text_budget import count_words

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# ava:ref scripts/content_lint/docs/lint-skill-descriptions.ava.okf.md#skill-desc-length-gate
_HARD_CEILING = 80
_SOFT_TARGET = 50


# ava:ref scripts/content_lint/docs/lint-skill-descriptions.ava.okf.md#skill-desc-scan-universe
def _skill_entries(root: Path) -> list[SkillFile]:
    index = SkillIndex.build(
        [
            root / "ava_builtins" / "skills",
            root / "ava_builtins" / "plugins",
            root / ".agents" / "skills",
        ]
    )
    seen: set[Path] = set()
    out: list[SkillFile] = []
    for entry in index.entries:
        if entry.skill_md is None:
            continue
        real = entry.skill_md.resolve()
        if real in seen:
            continue
        seen.add(real)
        out.append(entry)
    return out


def main(_argv: list[str] | None = None, *, repo_root: Path | None = None) -> int:
    root = repo_root or _REPO_ROOT
    errors = 0
    for entry in _skill_entries(root):
        skill_md = entry.skill_md
        if skill_md is None:
            continue
        rel = skill_md.relative_to(root)

        # ava:ref scripts/content_lint/docs/lint-skill-descriptions.ava.okf.md#skill-desc-scan-universe
        if entry.error is not None:
            print(f"{rel}: {entry.error}")
            errors += 1
            continue

        # ava:ref scripts/content_lint/docs/lint-skill-descriptions.ava.okf.md#skill-desc-length-gate
        description = entry.description
        if description is None:
            raise RuntimeError(f"skill index contract violated for {skill_md}")
        n = count_words(description)
        if n > _HARD_CEILING:
            print(
                f"{rel}: description is {n} units (CJK chars + non-CJK words), over the "
                f"{_HARD_CEILING}-unit hard ceiling — trim it (aim under {_SOFT_TARGET}); "
                f"the depth belongs in the skill body, not the index line."
            )
            errors += 1

        # ava:ref scripts/content_lint/docs/lint-skill-descriptions.ava.okf.md#skill-desc-identity-gate
        parts = entry.folder.relative_to(root).parts
        if (
            len(parts) == 4
            and parts[0] == "ava_builtins"
            and parts[1] == "plugins"
            and parts[3] == "skills"
        ):
            dir_name = parts[2]
        else:
            dir_name = entry.folder.name
        if entry.name is not None and match_key(entry.name) != match_key(dir_name):
            print(
                f"{rel}: frontmatter name {entry.name!r} does not fold to the "
                f"directory name {dir_name!r} — dash and underscore are one name; "
                f"rename the frontmatter name to match the directory (the directory "
                f"is the identity source)."
            )
            errors += 1
    if errors:
        print(f"\n{errors} SKILL.md frontmatter error(s).", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
