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
