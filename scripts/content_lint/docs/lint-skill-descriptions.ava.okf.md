---
type: doc
title: SKILL.md description gates — scan, length and identity
description: The scripts/content_lint/lint_skill_descriptions.py gates — which SKILL.md files it scans, how the description length is measured, and why the frontmatter name must fold to its directory.
tags: [scripts, lint, skills]
---

# SKILL.md description gates — scan, length and identity

`lint_skill_descriptions.py` guards every repo-shipped SKILL.md frontmatter at
merge time. The runtime loader reads every skill's frontmatter while an agent
assembles its system prompt, so a single malformed repo SKILL.md (an unquoted
`: ` in a value, a missing field) would otherwise crash unrelated agents
fleet-wide — this is the merge gate the loader cannot be for itself; the
runtime loader only skip-warns a malformed *externally-installed* skill. User
skills under `~/.ava/skills/` are outside the repo's control and degrade
gracefully at runtime. The repo-wide no-CJK gate (user ruling 2026-08-27)
already bans raw CJK anywhere in the repo, so this lint keeps the structural
gates only.

## Scan universe

Files come from `base.packages.skills.index.SkillIndex.build` — the same
builder the runtime loader mounts from, so the lint and the loader cannot
drift (doorplate ⑤). Roots: `ava_builtins/skills/`, `ava_builtins/plugins/`
and the repo's own `.agents/skills/` (`.ava/skills` and `.claude/skills` are
links back to it). Entries whose SKILL.md resolves to the same real file are
reported once. The 2026-08 malformed-frontmatter incident (an
`ava-serious-research` practice file three levels deep, unquoted `: ` in its
description) shipped green because the old glob scanned only one and two
levels; the depth fix is pinned by tests.

```ava-contract
id = "skill-desc-scan-universe"
kind = "invariant"
statement = "Every repo-shipped SKILL.md under ava_builtins/skills/, ava_builtins/plugins/ and .agents/skills/ is materialized through the same SkillIndex.build the runtime loader uses, so the lint cannot drift from the loader; entries resolving to the same real file are reported once, and an entry whose frontmatter fails the runtime parse or lacks required fields is reported as an error and skipped for the remaining gates."
code = ["scripts/content_lint/lint_skill_descriptions.py:_skill_entries", "scripts/content_lint/lint_skill_descriptions.py:main"]
tests = ["scripts/content_lint/tests/test_lint_skill_descriptions.py::test_three_deep_skill_with_bad_frontmatter_is_rejected", "scripts/content_lint/tests/test_lint_skill_descriptions.py::test_three_deep_skill_with_good_frontmatter_passes", "scripts/content_lint/tests/test_lint_skill_descriptions.py::test_missing_required_field_is_rejected"]
```

## Description length

A description is the index line an agent reads to decide whether to reach for
the skill — and for skills injected into the system prompt it sits in *every*
system prompt. The hard half of a two-tier rule is enforced here: over 80
units fails. The soft target of 50 units is NOT enforced here — the sweeper's
`skill-desc` class flags 50-80 descriptions for an agent to trim with
judgment; mechanical truncation in the prompt was the wrong fix (full
descriptions render, length is governed at the source instead).

Units come from the shared `count_words` in `base/packages/text_budget.py` —
the same CJK-aware measure used repo-wide: one CJK code point, or one
non-CJK run of letters and digits (underscores do not join runs), is one
unit. A flat char count is unfair across languages — 80 English characters
is ~12 words, 80 CJK characters are 80 meaningful characters — so a CJK
character and a word each count as one unit, which tracks information
content (and prompt tokens) far better.

```ava-contract
id = "skill-desc-length-gate"
kind = "invariant"
statement = "A description over the 80-unit hard ceiling prints one error and exits 1; the 50-unit soft target is not enforced by this lint — the sweeper's skill-desc class flags 50-80 descriptions for judgment. The length is measured through the shared count_words in base/packages/text_budget.py (one CJK code point or one non-CJK alphanumeric run is one unit; underscores do not join runs), and the constants move only as a deliberate change in the same PR, justified in the commit message."
code = ["scripts/content_lint/lint_skill_descriptions.py:_HARD_CEILING", "scripts/content_lint/lint_skill_descriptions.py:_SOFT_TARGET", "scripts/content_lint/lint_skill_descriptions.py:main"]
tests = ["scripts/content_lint/tests/test_lint_skill_descriptions.py::test_description_over_hard_ceiling_is_rejected", "scripts/content_lint/tests/test_lint_skill_descriptions.py::test_description_at_the_ceiling_passes_and_one_unit_over_fails"]
```

## Identity gate

The frontmatter `name:` must fold to the same key as the skill's own directory
(dash and underscore are one name). The check runs against the name the skill
converges to in the load dir: a plugin's root SKILL.md sits at
`plugins/<p>/skills/SKILL.md` in the source tree but lands at
`skills/<p>/SKILL.md` — the load-dir leaf is the plugin name, not the
`skills` container. The runtime loader refuses a mismatch at scan time; a
repo skill must never ship one.

```ava-contract
id = "skill-desc-identity-gate"
kind = "invariant"
statement = "The frontmatter name must fold (dash and underscore are one name) to the same key as the skill's load-dir leaf — for a plugin's root SKILL.md the leaf is the plugin name, not the skills container — and a mismatch prints one error and exits 1, matching the runtime loader's refusal."
code = ["scripts/content_lint/lint_skill_descriptions.py:main"]
tests = ["scripts/content_lint/tests/test_lint_skill_descriptions.py::test_frontmatter_name_not_folding_to_dir_rejected", "scripts/content_lint/tests/test_lint_skill_descriptions.py::test_plugin_root_skill_folds_against_plugin_name", "scripts/content_lint/tests/test_lint_skill_descriptions.py::test_dash_dir_and_dash_frontmatter_pass"]
```

Parent: [[scripts/content_lint/docs/content_lint.ava.okf.md]].
