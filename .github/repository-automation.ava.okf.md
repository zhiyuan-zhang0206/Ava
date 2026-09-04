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

## `workflows/ci-rerun.yml`

A failed completed CI run can dispatch one rerun of its failed jobs. The workflow
records its marker on the PR and refuses an unbounded rerun loop; a still-red run
returns to normal maintainer triage.

## `workflows/update-model-pricing.yml`

A daily schedule (plus manual dispatch) runs the strict provider adapters in
`scripts/update_model_pricing.py`. No change is a no-op. A verified source change
appends an effective-dated period on the fixed `ava-bot/model-pricing` branch and
opens or updates a review-only PR, then explicitly dispatches `ci.yml` because
GitHub suppresses recursive workflow events created by `GITHUB_TOKEN`.

The write-enabled job executes updater code from protected `main`; the mutable bot
branch is mounted only as an isolated candidate worktree, and only its catalog
file is passed as data to the trusted updater.

## `workflows/audit-branch-protection.yml`

A weekly schedule (plus manual dispatch) runs
`scripts/audit_branch_protection.py` with read access to GitHub's live branch
protection and workflow registry. The script derives the expected checks from
`.trunk/trunk.yaml`, then verifies exact required contexts, non-strict update
policy, admin enforcement, and active `ci.yml` / `ci-rerun.yml` workflows.

Verified drift keeps the run red and creates or updates one marker issue. A
could-not-verify result stays red without opening an issue, avoiding transient API
failures becoming false drift reports. The next successful audit comments on and
closes every open marker issue, making the alert loop self-healing.

## Key dependencies

- [[.github.ava.okf.md]] — parent overview and the protected CI check names.
- [[../scripts/scripts.ava.okf.md]] — audit and model-pricing implementations.
