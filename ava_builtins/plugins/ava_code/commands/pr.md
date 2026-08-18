---
description: Wrap up the current work into a PR — test, commit, push, watch CI
instruction-hint: optional PR title / context
---

Wrap up the current work into a pull request: run the tests relevant to the change locally (not just the pre-commit fast subset) and confirm they pass, commit with a clear message and a `Co-authored-by: Ava #<your agent id>` trailer, push the branch, and open the PR with its description in the file-tree format from .agents/skills/write-a-pr-description/SKILL.md (a file-tree diff marked A/M/D/R with ★ on the critical path, a prose data-flow supplement, and an explicit not-tested section). Then watch CI until it settles and report the result. Don't merge — leave it for review.
