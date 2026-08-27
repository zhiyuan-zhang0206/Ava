#!/usr/bin/env python3
"""Lint: SKILL.md frontmatter — must load; `description` length and identity.

Three gates over every repo-shipped SKILL.md:

1. **Frontmatter validity (hard fail).** The file must parse with the SAME
   parser the runtime loader uses (`shared.frontmatter.parse_frontmatter`) and
   carry the required `name` + `description` fields. This is the merge gate the
   runtime loader cannot be: every agent reads every skill's frontmatter while
   assembling its system prompt, so a single malformed SKILL.md (an unquoted
   `: ` in a value, a missing field) would otherwise crash unrelated agents
   fleet-wide. Repo skills are caught here; the runtime loader skips a malformed
   *externally-installed* skill with a warning (it cannot be CI-gated).

2. **`description` length (hard fail at 80 units).** A skill's `description`
   is the index line the agent reads to decide whether to reach for the
   skill — and for skills listed in
   `AVA_SKILLS_TO_INJECT_INTO_SYSTEM_PROMPT` it sits in *every* system
   prompt, so it has to stay short. The hard half of a two-tier rule:

     - hard ceiling 80 units — enforced here. A description that big is doing
       the skill body's job; cut it down.
     - soft target 50 units — NOT enforced here. The sweeper skill carries a
       `skill-desc` class that flags 50-80 descriptions for an agent to optimize
       with judgment (mechanical truncation in the prompt was the wrong fix —
       full descriptions render, length is governed at the source instead).

   Length is measured in *units*, not raw characters: one CJK ideograph = 1 unit,
   one whitespace-delimited run of non-CJK text (a word) = 1 unit. A flat char
   count is unfair across languages — 80 English chars is ~12 words, 80 CJK chars
   is 80 meaningful characters — so a CJK character and an English word each count as one unit,
   which tracks information content (and prompt tokens) far better.

3. **Identity consistency (hard fail).** The frontmatter `name:` must fold
   to the same key as the SKILL.md's own directory name (dash/underscore are
   one name — `shared.skill_names.SkillIdentity`). The directory is the
   skill's identity source; a frontmatter name that denotes a different
   skill would load under a name that is not its own, and the runtime
   loader now refuses it — so it must be caught here at merge time for
   repo-shipped skills.

Note: the repo-wide no-CJK gate (`scripts/lint_no_cjk.py`, user ruling
2026-08-27) supersedes the former English-primary description check — any raw
CJK anywhere in the repo fails that gate, so a CJK description is already
caught there. This script keeps the structural gates (parse / length /
identity) only.

Scans repo-shipped skills only (`ava_builtins/skills/`, `ava_builtins/plugins/*/skills/`, and the repo's own git-tracked `.agents/skills/` — `.ava/skills` and `.claude/skills` are links back to it), at both the root and the sub-skill level; user
skills under `~/.ava/skills/` are out of the repo's control (and degrade
gracefully at runtime).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from shared.skill_index import SkillFile, SkillIndex
from shared.skill_names import match_key

_REPO_ROOT = Path(__file__).resolve().parent.parent

_HARD_CEILING = 80
_SOFT_TARGET = 50

# CJK ideographs count one unit each; CJK punctuation is blanked (like Latin
# punctuation it is not its own unit); everything left splits into words.
_CJK_IDEOGRAPH = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_CJK_PUNCTUATION = re.compile(r"[\u3000-\u303f\uff00-\uffef]")


def length_units(text: str) -> int:
    cjk = len(_CJK_IDEOGRAPH.findall(text))
    latin = _CJK_PUNCTUATION.sub(" ", _CJK_IDEOGRAPH.sub(" ", text))
    return cjk + len(latin.split())


def _skill_entries() -> list[SkillFile]:
    """Every repo-shipped SKILL.md as a materialized index entry.

    The scan is `shared.skill_index.SkillIndex.build` — the SAME builder the
    runtime loader uses (doorplate ⑤), so the lint and the loader cannot drift.
    Roots: `ava_builtins/skills/` + `ava_builtins/plugins/` (a bare `skills/`
    at the repo root no longer exists — #864) and the repo's own `.agents/skills`
    (the open standard — `.ava/skills` and `.claude/skills` are links back to
    it, so scanning one of them would re-scan the same files through the
    other); entries whose SKILL.md resolves to the same real file are reported
    once.
    """
    index = SkillIndex.build(
        [
            _REPO_ROOT / "ava_builtins" / "skills",
            _REPO_ROOT / "ava_builtins" / "plugins",
            _REPO_ROOT / ".agents" / "skills",
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


def main() -> int:
    errors = 0
    for entry in _skill_entries():
        skill_md = entry.skill_md
        if skill_md is None:
            continue  # _skill_entries keeps only entries with a SKILL.md
        rel = skill_md.relative_to(_REPO_ROOT)

        # Gate 1: frontmatter must load with the runtime parser + carry the
        # required fields. The parse happened in the shared index (doorplate
        # ⑤) — the same builder the runtime loader mounts from.
        if entry.error is not None:
            print(f"{rel}: {entry.error}")
            errors += 1
            continue

        # Gate 2: description length.
        description = entry.description
        if description is None:
            # Index contract: error is None only when name+description parsed.
            raise RuntimeError(f"skill index contract violated for {skill_md}")
        n = length_units(description)
        if n > _HARD_CEILING:
            print(
                f"{rel}: description is {n} units (CJK chars + non-CJK words), over the "
                f"{_HARD_CEILING}-unit hard ceiling — trim it (aim under {_SOFT_TARGET}); "
                f"the depth belongs in the skill body, not the index line."
            )
            errors += 1

        # Gate 3: identity consistency — the frontmatter name is the display
        # declaration of the directory's identity (design R2-B). The runtime
        # loader verifies this at scan time and refuses a mismatch; a repo
        # skill must never ship one. The check runs against the name the skill
        # CONVERGES to in the load dir: a plugin's root SKILL.md sits at
        # plugins/<p>/skills/SKILL.md in the source tree but lands at
        # skills/<p>/SKILL.md — the load-dir leaf is the plugin name, not the
        # "skills" container.
        parts = entry.folder.relative_to(_REPO_ROOT).parts
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
