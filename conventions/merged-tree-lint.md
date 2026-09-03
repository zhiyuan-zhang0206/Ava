# merged-tree-lint

## What this is

`merged-tree-structure` is an informational job in `.github/workflows/ci.yml`.
For eligible pull requests, it runs the same structural pre-commit gates as
`backend-structure` against the prospective merge of `origin/main` and the PR
head. It is an early warning only: a finding does not block the workflow today.

## Why

Issue #1584 exposed a merge-emergent violation. A file was 738 lines on main,
then a PR added 75 lines, producing an 813-line file only in the Trunk combined
tree. `lint-code-structure` enforces an 800-line hard ceiling
(`scripts/lint_code_structure.py` sets `_HARD_CEILING = 800`), and the
`lint-code-structure` pre-commit hook runs that check.

The PR head passed its own checks because it was tested against a stale main.
The red result appeared only after queue admission, which wasted a combined-tree
round, bisection, and QA re-review. This job makes that kind of violation visible
while the PR is still being reviewed.

## How it works

The job checks out the PR head (`github.event.pull_request.head.sha`, full
history) and runs `git merge --no-commit --no-ff origin/main` — a real 3-way
merge staged in the index and working tree, with NO commit and no branch refs
created. The restore step then runs unconditionally: `git reset --hard HEAD`
clears the staged merge (including `MERGE_HEAD`) and `git clean -fd` removes
merged-only files, so the runner is back at the PR head before the job ends.

With the merged tree staged in the real index, `pre-commit run --all-files`
enumerates exactly the merged tree. The same `SKIP` list as
`backend-structure` avoids repeating hooks owned by other CI jobs.

A merge conflict is not a failed lint: the job records the conflicted paths
(`git diff --name-only --diff-filter=U`), marks the run skipped, and does not
run installation or pre-commit steps — the queue surfaces the conflict anyway.

## Reading a finding

The job remains green when pre-commit finds a problem. Read the failing hook and
file in the step log, then download the `merged-tree-structure` artifact. It can
contain `merge.out` (the merge transcript), `pre-commit.log`, and, for an
unmergeable PR, `conflicts.txt`.

Treat a finding with the same seriousness as a red `backend-structure` result,
then identify its source. A real merged-tree violation needs the main change and
the PR change together. A head-tree false positive would also be red in
`backend-structure` on the PR's own head and should be handled there instead.

## Promoting to a gate

The job never blocks today. Promote it only after all of the following are true:

- Informational runs have been observed for an agreed number of weeks and queue
  cycles.
- False positives and their outcomes are recorded in this document.
- The team agrees the artifact and log workflow is sufficient for reviewers.
- Branch protection is updated to require the check and both finding steps lose
  `continue-on-error`.

## Interaction with the queue

The Trunk Merge Queue still has the final say: its combined-tree test is
authoritative. `merged-tree-structure` is an early warning before admission, not
a replacement for the queue's final verification.
