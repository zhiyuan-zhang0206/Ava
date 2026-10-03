---
type: doc
title: "Repository Automation"
description: "GitHub-hosted maintenance loops outside the primary CI/release gates: one-shot failed-job reruns, reviewed model-price proposals, and branch-protection drift auditing."
tags:
  - ci
  - ops
  - infrastructure
---

# Repository Automation

## Proof workflow triggers

Proof workflows use path-filtered push/PR entry points; job predicates can
further restrict execution. `tests/ci/test_workflow_paths.py` checks every
workflow push/PR event for nonempty `paths` / `paths-ignore` or an explicit,
event-specific exemption, rejecting stale exemptions. Branch/tag/activity
filters alone do not qualify. Schedules and other event kinds are outside
that guard.

## `workflows/ci-rerun.yml`

A failed or cap-cancelled completed CI run can dispatch one rerun of its failed
jobs: `cancelled` is included alongside `failure`, because a job hitting its own
`timeout-minutes` cancels the run and the next attempt usually passes (cap-cancel
class). The workflow records its marker on the PR and refuses an unbounded rerun
loop; the single-attempt, superseded-head, and newest-run guards also bound the
residual — a deliberately cancelled run is re-run at most once, since GitHub
exposes no cancel reason to distinguish it. A still-red run returns to normal
maintainer triage.

The trigger whitelist is per-workflow: only `CI` is retried; every other workflow is
left to manual triage.

## `workflows/update-model-pricing.yml`

A daily schedule (plus manual dispatch) runs the strict provider adapters in
`scripts/model_registry/update_model_pricing.py`. No change is a no-op. A verified source change
appends an effective-dated period and synchronizes each provider plugin's flat
runtime rate to the period covering the run. The fixed
`ava-bot/model-pricing` branch opens or updates a review-only PR, then explicitly
dispatches `ci.yml` because GitHub suppresses recursive workflow events created
by `GITHUB_TOKEN`.

The write-enabled job executes updater code from protected `main`; the mutable bot
branch is mounted only as an isolated candidate worktree. Its archive and
provider sources are parsed as data by the trusted updater, never imported or
executed. Future effective windows are copied into the PR body for review.

## `workflows/dependency-audit.yml`

A weekly schedule (plus manual dispatch) runs `scripts/ci/dependency_audit.py`:
`uv audit` over `uv.lock` (severity looked up in OSV, since its JSON carries
none), `npm audit` over `ui/web`, and the version constants Ava downloads outside
the lockfiles (zonky Postgres, pgvector, wal-g, otelcol-contrib, uv, Grafana)
against each upstream's newest release. One issue, found by its marker, is
opened or updated while any advisory is high, critical or ungradable or any
pin is behind, and closed on a clean run. The PR-time `dependency audit` job in
`ci.yml` stays informational and never blocks. The report changes no pin:
moving one is a dependency upgrade that needs approval.

## `workflows/audit-branch-protection.yml`

A weekly schedule (plus manual dispatch) runs
`scripts/audit/branch_protection.py` with read access to GitHub's live branch
protection and workflow registry. The script derives the expected checks from
`.trunk/trunk.yaml`, then verifies exact required contexts, non-strict update
policy, admin enforcement, and active `ci.yml` / `ci-rerun.yml` workflows.

Verified drift keeps the run red and creates or updates one marker issue. A
could-not-verify result stays red without opening an issue, avoiding transient API
failures becoming false drift reports. The next successful audit comments on and
closes every open marker issue, making the alert loop self-healing.

## Key dependencies

- [[.github.ava.okf.md]] — parent overview and the protected CI check names.
- [[../scripts/docs/scripts.ava.okf.md]] — audit and model-pricing implementations.
