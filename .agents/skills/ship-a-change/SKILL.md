---
name: ship-a-change
description: Ships Ava repo changes through worktree, commit, rebase, PR, CI, merge queue, and cleanup. Use for every code change in this repo, even when the edit looks trivial or the user asks only to commit.
---

# Ship a change

The full development workflow from scope to merge.

## Scope / plan

Don't deliberately split into phases. Adjacent refactors, obvious bugs, and
small issues fixable in passing — **do them together**. Forcing a tidy scope
lets small problems accumulate.

Boundary: don't aimlessly refactor whole files — limit to "near the range of
this commit". If the spread exceeds what a single commit can carry, stop and
align with the user first.

Plan docs are scaffolding for execution-time reference; `git rm` after
implementation. **Never** merge them into main. Durable docs are only
`okf/` (source of truth), `conventions/` (policy/reference) and
`.agents/skills/` (the procedures you follow, this file included).

## Worktree + PR (mandatory)

**Every change must be made in an isolated worktree and merged via PR. Direct
push to main is forbidden.**

### Branch model

| Branch | Purpose | Who can merge |
|---|---|---|
| `main` | Release branch; production auto-update pulls from here | Agent (with user authorization) |
| `ava-<id>-<task>` | Feature branch, created from main | Developer |

### Rebase-only policy (mandatory)

**Linear history only. Merge commits are forbidden.** Always rebase your
feature branch onto `main` — never merge `main` into your feature branch.
This keeps the commit graph flat and history readable.

Before pushing or opening a PR:
```bash
git fetch origin main && git rebase origin/main
```

If conflicts arise during rebase, resolve them in your branch, then
`git rebase --continue`. After a successful rebase, force-push is allowed
for your own feature branch:
```bash
git push --force-with-lease
```

**Merge method is squash.** The Trunk queue squashes each PR into one
commit on `main` (user ruling: keep squash). The branch keeps its own commit
structure for review; `main` gets one commit per PR.

### Merge queue (mandatory)

PRs merge through the **Trunk** merge queue — not by direct merge.
**QA gate:** the queue's merge conditions require the
`qa-approved` label — a PR without it is never merged, even with green CI
and an enqueued position (it waits in the queue until QA labels it). The
label is applied by QA / the maintainers only, and only on a final PASS /
PASS-with-nits conclusion; BLOCK / CONDITIONAL never carry it and a later
BLOCK removes it immediately. Authors never self-apply the label; review and
QA evidence must match the exact head SHA, and any new commit after a PASS
still requires a delta re-review before the label is (re)applied. Submitting is
`.venv/bin/python scripts/ci_utils.py <PR#> --wait --merge` (requires
`~/.trunk/api-token`; ci_utils polls to green, submits, then waits for the
queue to land the PR). Trunk batches queued PRs into one test draft
verification: a `trunk-merge/pr-<n>/...` branch carrying the combined tree,
CI running on it via the normal `pull_request` event (ci.yml's draft-skip
exempts that branch prefix). On green every PR in the batch lands (squash
merge); a red batch auto-bisects to evict the culprit. **You no longer
rebase-and-repoll when `main` moves**: the queue verifies the combined tree
that actually lands.

Still on you:
- **Conflicts** — the queue cannot rebase a conflicting PR. Resolve locally
  (`git rebase origin/main`), force-push, re-enqueue.
- **A red PR** — CI failures on your branch are yours; fix, re-push, re-enqueue.
- **User-review PRs are never enqueued.** A PR awaiting the user's verdict
  stays manual until they say go.

### Steps

1. `git worktree add -b ava-<id>-<task> .worktrees/ava-<id>-<task> main`
2. Develop and commit in the new worktree (run `bash scripts/setup-worktree.sh`
   on first use)
3. Rebase onto latest main: `git fetch origin main && git rebase origin/main`
4. Run local tests before pushing — see [`.agents/skills/run-local-tests/SKILL.md`](../run-local-tests/SKILL.md).
   An explicit user CI-only constraint overrides local execution; record the
   skipped local gates and confirm that the corresponding CI checks actually run.
5. Push branch → `gh pr create --base main`
6. Wait for CI all-green: `.venv/bin/python scripts/ci_utils.py <PR#>`
   Also detects merge conflicts — if your PR has conflicts, CI won't start.
   Rebase first when the verdict is MERGE_CONFLICT.  Poll repeatedly
   (e.g. every 30–60 s) until ALL_PASSED before merging.
   A `NO_WORKFLOW_RUNS` verdict means Actions never scheduled — only a GitHub
   App reported, and nothing is queued to explain it. That is not green: find
   out why the workflow did not run. (A run that is queued but has not attached
   its check yet reports PENDING, not this — keep polling.)

   **Long waits: launch the reference CI watcher instead of polling in-turn.**
   `reference/ci_watcher.py` wraps `check_ci()` and wakes you with exactly one
   message when CI settles (green, red, conflict, or no-workflow-run — never a
   silent timeout). Configure it by string-replacing its placeholders, then
   `ava.watcher.launch(code, timeout="3h", name="ci-watch-<pr>")`:

   ```python
   import ava
   code = ava.files.read(
       "<repo>/.agents/skills/ship-a-change/reference/ci_watcher.py"
   )
   code = code.replace('REPO_ROOT = ""', f'REPO_ROOT = "{worktree}"')
   code = code.replace('PR_NUMBER = ""', f'PR_NUMBER = "{pr}"')
   code = code.replace('CI_UTILS = ""', f'CI_UTILS = "{worktree}/scripts"')
   code = code.replace("WATCHER_ID = 0", f"WATCHER_ID = {ava.self.AGENT_ID}")
   ava.watcher.launch(code, timeout="3h", name=f"ci-watch-{pr}")
   ```

   Never write an ad-hoc `gh pr checks` + exit-code poll: `gh pr checks`
   exits non-zero when a check FAILS, so a `returncode == 0` condition never
   fires on red and the PR can sit failed until the watcher times out.
   `check_ci()` (and the reference watcher) report FAILED as an explicit
   verdict instead.
7. **Submit** — `.venv/bin/python scripts/ci_utils.py <PR#> --wait --merge`
   (polls to green, submits to the Trunk queue, then waits for it to land).
   No merge-base check and no rebase-and-repoll loop: the queue tests the
   combined tree on the latest `main`. A conflicting PR is bounced — `git
   fetch origin main && git rebase origin/main`, force-push, resubmit. A
   failed queue attempt on the same head SHA is not retried by Trunk
   (instant-fail): change the SHA (rebase) before resubmitting.
8. Verify it landed (when not using `--merge`): `gh pr view <PR#>` →
   state `MERGED`; the queue may take 10-30 min. Before removing the
   worktree, run `python scripts/check_worktree_remove.py <path>` from the
   dev clone and **abort the removal if it reports live sessions or
   processes anchored under the path** — a cluster-owned session anchored
   there (a schedule launched by a gateway that ran from the worktree,
   issue #194) dies silently when the worktree's `.venv` disappears, and
   the schedule's DB row keeps claiming `running`. Then `git worktree
   remove <path>` and delete the remote branch (`git push origin --delete
   <branch>`) to clean up — Trunk does not always auto-delete branches.

Exception: skip PR only when the user explicitly says "push directly".

## PR description

See **[`write-a-pr-description`](../write-a-pr-description/SKILL.md)** for the full spec — must have
file-tree diff with ★ critical paths + prose data flow.

## When a pre-commit hook cannot run: `SKIP=`, never `--no-verify`

Four hooks shell out to `npx` against `ui/web/node_modules`:
`frontend-tsc`, `frontend-eslint`, `frontend-vitest`, `types-codegen-fresh`.
A fresh agent worktree has no `ui/web/node_modules` and often cannot fetch
packages, so all four fail there — identically on a clean `main`, with no change
of yours involved.

Skip **those four by name**, so every other hook still runs:

```bash
SKIP=frontend-tsc,frontend-eslint,frontend-vitest,types-codegen-fresh git commit -m "..."
```

**Never reach for `--no-verify`.** It is not "skip the broken hook" — it disables
*every* hook at once, including the lints that have no other local gate
(`lint-ava-okf`, `lint-doc-symbols`, `lint-doc-anchors`, `lint-doc-roster`,
`lint-agents-md-size`, `lint-skill-*`, `lint-fail-fast`, `lint-no-os-environ`,
…). The failure mode is
silent: the commit succeeds, and you learn nothing about what you turned off.

Say in the commit message or PR body which hooks you skipped and why — a reader
who sees `SKIP=` named explicitly can judge the gap; one who sees nothing assumes
the full gate ran.

If a hook fails for a reason that is *not* the sandbox (a real lint error), fix
the error. Skipping is only for a hook this machine cannot execute at all.

## Post-merge

Merge proves repository integration, not production health. Deployment is a
separate, explicitly authorized operation by one designated operator; follow
`ava-self-development` for rollout and recovery verification. Contributors and
QA agents do not launch competing updates or production fixes.

Before merge, mandatory: `grep -rn "<old-name>" conventions/ future/ AGENTS.md`
to zero out references. Docs go in the same PR as code — **don't** leave a
"follow-up doc commit". Not just string replacement: also think "does this
change alter what the docs are trying to convey".

How to report after the merge — candidate next steps and what to leave out — is
in [`communicating-with-user.md`](../../../conventions/communicating-with-user.md).
