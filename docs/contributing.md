# Contributing to Ava

This is the entry point for coding agents contributing to Ava. Start from the
user's goal and authorized scope. Inspect existing behavior before editing;
discuss unresolved design, contract or dependency choices with the user. Follow
[AGENTS.md](../AGENTS.md) for project invariants.

## Find the owner

Read the relevant component's local `docs/` and the [OKF index](index.ava.okf.md).
Use `scripts/audit/where_used.py` before changing a symbol or contract so the
change includes real consumers, tests, patch targets and current documentation.
Cross-cutting rules live in [conventions](conventions/README.md); historical
[decisions](decisions/README.md) and [incident analyses](postmortems/README.md)
explain past choices and escapes rather than imposing today's workflow.

## Prepare an isolated checkout

Use a development clone and a branch from current `main`. Preserve existing work.
The helper `bash scripts/setup-worktree.sh <task>` prepares a worktree, locked
dependencies and its own real `.venv`; follow its final `worktree ready:` path.
A fork or another isolated Git setup is also valid when it preserves the same
environment boundaries. Do not edit a running production checkout or share its
virtualenv. See [development setup](conventions/dev-setup.md) and
[Git hook setup](conventions/runbook.md#git-hooks-pre-commit--pre-push).

Pytest isolates its environment. Other development tools importing application
code must set a temporary `AVA_HOME` before import; unset `AVA_HOME` selects the
default home and can target a running deployment.

## Change and verify

Implement a focused change with its related tests and current documentation.
Keep shared facts in their owning module; use existing third-party capabilities
rather than creating a second framework or registry. Resolve generated-file
conflicts by regenerating from the source, not editing derived output.

Run affected test files and relevant contract consumers. The [testing guide](conventions/testing.md)
has Python, frontend and e2e commands, environment details and cleanup guidance.
Full suites run in CI. The [migration guide](../db/docs/migrations.md) covers
schema changes, immutable migration history and baseline verification.

### Locality gate (meizel)

`python -m meizel check` judges every first-party dependency against the
`meizel.toml` manifests (see [the checker](../meizel/docs/checker/checker.ava.okf.md)).
A commit runs `check --changed` on the staged files, a push and CI run the whole
`check`, and CI also compares recorded debt with the base branch.

Directories that still carry recorded debt are strict-to-be: the recorded
violations pass, anything new fails, and the debt may only shrink
([recorded debt](../meizel/docs/checker/debt.ava.okf.md)).

- Fix a violation instead of recording it. Move the code, use the public door,
  inject the collaborator, or declare the edge in the manifest of the directory
  where the two sides meet.
- After a rebase or a move, run `python -m meizel check`, then `python -m meizel
  sync --declare` for `undeclared-edge` and `not-exported` (it declares only the
  legal new dependencies of your changed files and prunes stale ones). An edge
  that closes a cycle, a private name, an underscore module or a restricted form
  is not declared; change the code. Run `python -m meizel debt prune` for
  `debt-paid`. After moving a file, stage the move and run `python -m meizel debt
  carry` so its recorded debt follows it (it relocates entries, never adds one).
- `python -m meizel debt record` is for a deliberate exception that has been
  discussed with the maintainers and is explained in the PR. It is never the way
  to make a failing check pass.
- When a change pays debt down, run `python -m meizel debt prune` in the same
  change so the baseline drops with the fix. `python -m meizel debt` shows what
  is left.
- CI fails when the recorded total grows against the base (`debt diff --on-growth=fail`).
  The one waiver is decided by path, not by a reader: a change that edits meizel's own rule code
  (Python under `meizel/` outside tests) reports growth as a warning, because widening a rule
  grows the baselines.
- Patching is debt of every kind: `first-party-patch`, `stdlib-patch`, `third-party-patch`,
  `env-patch` and `unresolved-patch` (a target the imports do not resolve). Do not add one;
  pass the dependency in as an explicit parameter
  ([patching](../meizel/docs/checker/patching.ava.okf.md)). When a symbol is renamed, `debt carry
  --rename-target OLD=NEW` moves its entries.
- `meizel.policy.toml` lists every allowance a manifest cannot grant itself (`[external]` sites,
  `[loads]` sites, default-argument injections). Do not edit it unless the task names the entry;
  CI prints every change to it ([the policy file](../meizel/docs/checker/policy.ava.okf.md)).

Before submitting, review your own diff with
[review-contribution](../.agents/skills/review-contribution/SKILL.md). It routes
to current project and domain rules; another reviewer agent, a reviewer persona,
a fixed approval comment and a review-publishing step are not required. If the
review exposes an unresolved design choice, discuss it with the user before
changing that choice.

## Submit the PR

Push the branch and open a PR against `main`. Explain the problem, resulting
behavior, validation and known gaps. Use a file tree or control-flow explanation
when it helps readers understand a larger change; no fixed template is required.
Address relevant CI failures and review feedback. Distinguish successful execution
from missing, skipped or incomplete verification. Maintainers choose how to merge.

Keep the checkout while it contains needed work. Before removing a worktree,
check for live sessions or processes anchored there with
`scripts/host_ops/check_worktree_remove.py`; preserve it if the check cannot establish that
removal is safe. See [development setup](conventions/dev-setup.md).

A merge proves repository integration, not production health. Runtime rollout is
a separately authorized operator action with compatibility and recovery checks;
see [kernel change and deployment safety](../.agents/skills/ava-self-development/SKILL.md).
