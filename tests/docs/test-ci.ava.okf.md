---
type: doc
title: "Test CI integration"
description: "Ownership of test and validation execution across Git hooks and CI."
tags:
- evaluation
- quality-assurance
---

# Test CI integration
- `.github/workflows/` — GitHub Actions runs the full suite
- pre-commit lints what the commit changed; pre-push runs pyright (branch-changed `.py` files only, never full-repo locally — user ruling 2026-09-22), frontend tsc, whole-project eslint, **full frontend vitest**, a branch-diff rerun of the pre-commit stage over `merge-base(origin/main,HEAD)..HEAD` (catches rebase/cherry-pick/merge commits that never ran pre-commit), and a re-check of the artifact hooks whose inputs the branch deleted (a delete-only diff never reaches a `files:`-filtered hook) (`.pre-commit-config.yaml`). Neither stage runs pytest.
- CI runs all non-e2e tests + e2e + coverage thresholds
- CI owns every local check except the warn-only hook-installation check: `backend-structure` runs structural lints plus a conditional, explicit codegen segment; `backend-static` owns pyright and the pure unit lane's execution/count/coverage evidence, and `frontend` owns tsc, eslint and vitest. The classify-independent `doc-lints` job covers docs-only PRs too. See the [CI runbook](../../docs/conventions/runbook.md#ci-continuous-integration) for ownership and selector safety nets.
