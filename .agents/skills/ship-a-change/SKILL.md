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

A conflict in a generated file is never merged by hand: take either side, finish
the rebase, and run its generator (the pre-push branch-diff hook re-runs the freshness
hooks and fails a push that skips it). The files: `base/events/registry.md`
(`scripts/codegen/gen_event_registry.py`), `base/host/env/config_lite_table.json`
(`scripts/codegen/gen_config_lite_table.py`), the generated block of
`pyproject.toml` (`scripts/codegen/gen_pyright_test_environments.py`),
`ui/web/src/lib/types-generated.ts` (`scripts/codegen-types.sh`) and
`ui/web/src/lib/constants-generated.ts` (`scripts/codegen/dump_frontend_constants.py`).

**Merge method is rebase (mandatory, user ruling 2026-09-27).** The Trunk
queue rebase-merges each PR: every commit on the branch lands on `main` as is,
so each commit must stand on its own — a conventional subject, a body that
says why, and green on its own tree as far as you know. GitHub allows rebase
merges only, and the Trunk queue's merge method (app.trunk.io queue settings)
is Rebase; the two must change together.

**Stacked PRs.** Because commits land individually, a stack lands as one queue
submission: rebase the whole stack onto `main`, retarget the TOP PR to `main`,
and submit only that one — every layer's commits land in order, one CI and one
queue round instead of one per layer. Then close the lower PRs with a comment
naming the PR they landed through. Submitting a lower layer on its own is fine
too; the next layer's rebase onto `main` drops the already-applied commits.

### Merge queue (mandatory)

PRs merge through the **Trunk** merge queue — not by direct merge. Submitting is
`.venv/bin/python scripts/ci_utils.py <PR#> --wait --merge` (requires
`~/.trunk/api-token`; ci_utils polls to green, submits, then waits for the
queue to land the PR). Trunk batches queued PRs into one test draft
verification: a `trunk-merge/pr-<n>/...` branch carrying the combined tree,
CI running on it via the normal `pull_request` event (ci.yml's draft-skip
exempts that branch prefix). On green every PR in the batch lands (rebase
merge); a red batch auto-bisects to evict the culprit. **You no longer
rebase-and-repoll when `main` moves**: the queue verifies the combined tree
that actually lands.

Still on you:
- **Conflicts** — the queue cannot rebase a conflicting PR. Resolve locally
  (`git rebase origin/main`), force-push, re-enqueue.
- **A red PR** — CI failures on your branch are yours; fix, re-push, re-enqueue.
- **GitHub-limbo runs** — a run stuck `queued` with zero jobs never materializes
  and no GitHub API can cancel it; it holds an otherwise green rollup PENDING
  until the watch times out. `ci_utils` names the runs loudly; when they are the
  ONLY obstacle the operator escape hatch is `--force` (inspect the named runs
  first; real pending / failed / unreadable evidence still blocks — task #3275).
- **User-review PRs are never enqueued.** A PR awaiting the user's verdict
  stays manual until they say go.

### Steps

1. `bash scripts/setup-worktree.sh <task>` — the only way to create a worktree;
   run it from the main clone or any worktree. It fetches `origin/main`, creates
   `.worktrees/<task>` on branch `ava-<task>` under the main clone (`--branch NAME`
   / `--base REF` override), builds the worktree's own real `.venv`, runs the locked
   install and `npm ci` for `ui/web`, and fails unless the shared hooks, the
   editable-install guard and a clean `git status` hold. The last stdout line is
   `worktree ready: <path> (branch <branch>)`; `cd` there (a script cannot change
   your shell's directory). Re-running it only re-bootstraps; if it stops midway the
   worktree is kept, so run `bash scripts/setup-worktree.sh` with no argument inside
   it to resume. Never hand-make a worktree with `git worktree add`. A worktree
   made by another tool (Claude Code's own lands in `.claude/worktrees/<name>/`
   with no dependencies) is completed by the same no-argument run inside it; the
   main clone is refused.
2. Develop and commit in that worktree. Its `.venv` is a real directory under the
   checkout, **never a symlink** to a shared venv (`ln -s ~/Ava/.venv .venv`): a
   later `uv sync` then writes through the symlink and re-points the shared venv's
   editable `.pth` at this worktree — breaking every other checkout that uses that
   venv (pyright phantom-error storms; a prod exec outage). Troubleshooting only
   (the script already does this; worktree uv iron rule in
   [runbook](../../../conventions/runbook.md)):
   `env -u VIRTUAL_ENV python scripts/host_ops/guard_editable_venv.py . && env -u VIRTUAL_ENV uv sync --frozen`,
   then confirm `.venv/lib/python3.12/site-packages/_editable_impl_ava.pth`
   names this worktree. For a test-only run with no worktree venv of its own,
   reuse another worktree's real venv instead — see
   [run-local-tests](../run-local-tests/SKILL.md).
3. Rebase onto latest main: `git fetch origin main && git rebase origin/main`
4. Run local checks on only what you changed before pushing (pytest on the
   affected test files, `-n 2`; pyright on the changed files only); full suites and whole-repo pyright run only in CI (including for
   `base/` changes; user ruling 2026-09-22, pyright included 2026-10-01) — see [`.agents/skills/run-local-tests/SKILL.md`](../run-local-tests/SKILL.md).
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
   silent timeout). Arm it from an agent-profile process carrying the runner
   DB and Redis URLs; a secured default-home profile-less launcher is refused
   before session creation. Configure it by string-replacing its placeholders,
   then `ava.watcher.launch(code, timeout="3h", name="ci-watch-<pr>")`:

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

   The watcher persists the settled verdict to `ci-verdict-<pr>.txt` in your
   workspace before it tries to deliver, and retries transport failures for
   ~10 minutes:
   an update wave (`python -m cli.fleet_update`) refuses connections for minutes —
   longer than any single send survives. If no wake arrives, read that file;
   the verdict is there. For a persistent owner-URL config error, delivery
   stops after one attempt and the exit notice names the absolute verdict
   path and cause.

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
   worktree, run `.venv/bin/python scripts/check_worktree_remove.py <path>` (a python without psutil exits 3, no verdict; it reads this machine's own `$AVA_HOME`, else `~/.ava`, so run it bare, without an `AVA_HOME=<tmp>` prefix) and
   **abort the removal if it reports live sessions or
   processes anchored under the path** — a cluster-owned session anchored
   there (a schedule launched by a gateway that ran from the worktree,
   issue #194) dies silently when the worktree's `.venv` disappears, and
   the schedule's DB row keeps claiming `running`. The scan skips the
   invoking process chain, so running the guard from inside the worktree
   does not self-refuse (issue #3685). Then `git worktree
   remove <path>` and delete the remote branch (`git push origin --delete
   <branch>`) to clean up — Trunk does not always auto-delete branches.

Exception: skip PR only when the user explicitly says "push directly".

## PR description

See **[`write-a-pr-description`](../write-a-pr-description/SKILL.md)** for the full spec — must have
file-tree diff with ★ critical paths + prose data flow.

## When a pre-commit hook cannot run: `SKIP=`, never `--no-verify`

The commit-stage `types-codegen-fresh` hook needs `ui/web/node_modules`.
A fresh worktree without these dependencies can fail this hook even on clean
`main`. The frontend tsc, whole-project eslint and vitest hooks run at pre-push, and
the changed-files eslint hook at commit; each reports a visible skip when tooling
is unavailable.

If dependencies cannot be installed, skip **only that hook by name**, so every
other commit hook still runs:

```bash
SKIP=types-codegen-fresh git commit -m "..."
```

**Never reach for `--no-verify`.** It is not "skip the broken hook" — it disables
*every* hook at once, including the lints that have no other local gate
(`lint-ava-okf`, `lint-doc-symbols`, `lint-doc-anchors`, `lint-doc-roster`,
`lint-agents-md-size`, `lint-skill-*`, `lint-no-os-environ`,
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
`ava-self-development` for rollout and recovery verification. Contributors
do not launch competing updates or production fixes.

Before merge, mandatory: `grep -rn "<old-name>" conventions/ future/ AGENTS.md`
to zero out references. Docs go in the same PR as code — **don't** leave a
"follow-up doc commit". Not just string replacement: also think "does this
change alter what the docs are trying to convey".

How to report after the merge — candidate next steps and what to leave out — is
in [`communicating-with-user.md`](../../../conventions/communicating-with-user.md).
