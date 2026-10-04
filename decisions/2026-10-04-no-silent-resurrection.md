# A PR may not silently resurrect lines main deleted in the last 30 days

## Context

#4245 (6ecfe14c3) deleted `AVA_DELIVERY_WATCHDOG_ALERT_GRACE_SECONDS` and the `delivery_watchdog_fields`
code around it. #4207 (151bc92fe) then landed on main after replaying a stale branch through a conflict
resolution, and carried the old content of four of those files back. A rebase merge queue does not catch
this: the resurrected code compiles and its tests pass, so every existing gate is green. Asking agents to
re-check their rebases is a rule, and rules are not a mechanism.

## Decision

A required PR check (`no-silent-resurrection`, `scripts/ci/no_silent_resurrection.py`, stdlib + git only)
compares the lines a PR adds with the lines main deleted in the last N days (default 30). A PR that adds a
run of lines that one main commit deleted fails unless a commit message in the PR carries a
`Resurrects: <reason>` line (optionally naming the deleting sha or a path).

- Comparison is by line text, not path, so a file move does not hide a resurrection and a moved file is not
  one. A line a commit deleted and re-added in the same commit is not counted as deleted.
- A line still present anywhere in the base tree is not dead and never counts.
- Minimum unit: a run of consecutive added lines with at least two meaningful lines, or one line carrying a
  distinctive identifier. Blank lines, comments, imports, brackets and short generic lines are noise and only
  bridge a run.
- Lock files and generated files (openapi.json, types-generated.ts, registry.md, config_lite_table.json, ...)
  are skipped: a resurrected generated file is triggered by its source, which this check does see.
- `git revert` messages (`This reverts commit <sha>`) allow the hits that sha deleted.

## Alternatives rejected

- A rule in agent instructions: the incident happened with the rule's audience in the loop; nothing fails when
  it is forgotten.
- Path-only matching (`git log --diff-filter=D`): misses a file moved then resurrected, and flags every
  `__init__.py` or `README.md`.
- A line-level ban without the alive and run filters: common idioms (`return None`, `)`) deleted anywhere
  would fail unrelated PRs.
- A blanket allow label on the PR: not visible in the history. The reason lives in the commit message, next
  to the code that resurrects.

## Consequences

- A deliberate revert or restore costs one commit-message line.
- The check reads 30 days of main history (about 110 MB of patch text, ten-odd seconds), so the job checks out
  full history like `classify`.
- Matching is textual: a rename-and-edit of a deleted function under a new name is not caught.
