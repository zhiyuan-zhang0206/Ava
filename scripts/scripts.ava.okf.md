---
type: doc
title: "Scripts"
description: "`scripts/` is Ava's ops and engineering toolset — installation / provisioning, linting, CI / release, code generation, OKF graph building, cluster startup, etc., one-shot or hook-driven scripts. Not runtime code, but development and deployment infrastructure around the repo."
tags:
  - tool
  - ops
  - ci
---

# Scripts

## What is it

`scripts/` contains Python and shell engineering tools invoked by developers, git hooks, CI, and the CLI for installation, validation, and release.

## Script Categories

### Lint (`lint_*.py` + `check_doc_references.py`)
The full linter inventory — what each one enforces and where it runs: [[scripts/lint-scripts.ava.okf.md]].

### OKF Graph Toolchain
- `build_okf_data.py` — bundle → `graph_data.json` (nodes / tree edges / cross edges)
- `serve_okf_viz.py` — renders interactive graph locally
- `fix_okf.py`, `migrate_okf.py`, `fix_frontmatter.py` — batch fix / migrate

### Host Dependencies and Development Setup
Cluster initialization belongs to `ava start`; `ava start --worktree` creates an
isolated development home. These scripts prepare host tools or development
checkouts; they do not provide another cluster initialization path.
- `guard_editable_venv.py` — dependency-free worktree preflight that refuses symlinked/external virtualenvs and cross-checkout editable-install records before a sync can mutate them
- `install-cli-tools.sh`, `setup-worktree.sh`, `worktree.sh`
- `provision/` — `database.sh` / `node.sh` / `toolchain.sh` / `install-playwright.sh` (the Dockerfile's eval-image layer) / `install-system.sh` (Linux Debian/Ubuntu host tools: Python 3.12 + build tools, then composes the other scripts) / `_lib.sh`. Consumers include the `Dockerfile` and `install-cli-tools.sh`.
- `cli/python_install.py` handles source dependency synchronization: canonical lock validation, host-index transport with locked hashes, and the real editable checkout. Existing machine uv/pip settings are reused without rewriting the lock. Immutable release preparation uses its separately captured inputs. See [[cli/python-install.ava.okf.md]].

### CI / Release / Migration
- `qa_gate.py` + `qa_receipt.py` — evaluates exact-head QA evidence from GitHub; synthetic queue exemptions require verified Trunk identity, same-repository draft targeting main, and a complete normal or `-bisection` test ref (see [receipt contract](../conventions/qa-approval-receipt.md))
- `ci_utils.py` — polls PR CI status + conflicts; separates workflow checks from GitHub App ones so a suite that never ran cannot read as green (`NO_WORKFLOW_RUNS`) and a draft PR's gate-skipped suite reports `NOT_READY`; submits green, labeled PRs through Trunk with a cooldown, one retry, and terminal-state polling
- `coverage_gates.py` — backend CI coverage gates: the combined 85% line-rate over the core domains (agent/ava/cli/gateway/shared/ui) plus per-risk-domain minimum line floors for ops/services/ava_builtins, read from the combined shard `coverage.json`
- `audit_branch_protection.py` — read-only comparison of live GitHub branch protection / workflow activation against the required checks declared in `.trunk/trunk.yaml`; exit status distinguishes verified drift from an API/tool failure
- `build_app_update_manifest.py` — turns signed Tauri desktop archives into the app's static `latest.json`; unsigned release runs get an empty platform map
- `update_model_pricing.py` + `plugin_price_sync.py` — fail-closed official price checks (page columns map onto the catalog entries they price; a retired column closes onto its successor's published rates as the entry's future period) and conservative archive-to-plugin synchronization of complete period/tier/window declarations for reviewed pricing PRs
- `refresh_test_durations.py` — records isolated CI-shaped pytest timing shards, retries
  only a failed measurement, and atomically merges a complete artifact set into
  `.test_durations`
- `test_selector.py` — stdlib-only, read-only PR selector that reverse-maps
  static direct test imports, preserves conservative full-suite escapes, and
  emits selection decisions consumed by `ci.yml` (enforce by default, optional shadow)
- `prepush-guard.sh` — host-wide flock/load guard for pyright, tsc, eslint and vitest; loud skips, real failures propagate. `provision/check_git_hooks.py` warns on missing hooks or interpreter drift.
- `release_cut.py`, `check_cross_branch_migrations.py`, `migration_smoke.py`, `test_migrations_apply.sh`
- `post_deploy_visual_check.py` — read-only five-surface production visual gate: a gateway `started_at` change distinguishes deployment waves from daily sentinels, the repo-pinned Playwright Chromium (headless, host-local) captures desktop/narrow light/dark combinations after an explicit settle predicate, shared structural probes fail P0, and stable two-frame pixel drift on static crops is attributed to the golden-to-wave frontend diff. It writes artifacts and exit codes only; the invoking agent owns notifications. Golden updates require an audited `--accept-wave`. Its engine-agnostic capture
matrix lives in `post_deploy_visual_matrix.py`, shared with the blocking CI
preview gate (`tests/e2e/test_preview_visual_gate.py`); combinations that
declare minimum visible counts (home desktop's asynchronously mounted inspector
aside) wait bounded for those counts before probing, all others measuring
immediately.
- `backfill_llm_usage_hourly.py` — operator-run one-shot that folds the frozen 2026-08-28 cold-archive `llm_usage` JSONL extract into (UTC hour x model) totals and upserts them into `llm_usage_hourly`, the restored historical LLM usage/cost curve for the window Loki's 7d retention lost; re-runnable, and never called by the migration that creates the table

### Code Generation
- `codegen-types.sh`, `check-types-fresh.sh`, `dump_openapi.py`, `generate-ui-page.py`, `dump_event_fixtures.py`

### Startup / Deployment / Multi-host
- `start_agent.py` (derives an agent via gateway `/api/agents`), `start_gateway.py` (directly starts the gateway FastAPI body, ≈ `.venv/bin/python -m gateway`) — **the latter does not derive an agent**
- `multihost/` (`multihost.py` + `agent_runner_entrypoint.sh`), `preview/` (daily deployment + checkpoint rebuild)
- `cloud-bench-bootstrap.sh`, `metrics.py`
- `rotate_cluster_secret.py` — emergency control-plane `AVA_CLUSTER_SECRET` bearer rotation on the gateway machine (default `--dry-run`); it does not change the data plane
- `rotate_data_plane_secrets.py` — routine gateway-local data-plane rotation (`--scope admin`, `runner`, or `both`) with a 0600 recovery state
- `restore_drill.py` — decrypts a managed database backup, restores it into throwaway Postgres, and verifies schema, checkpoint counts, and a readable conversation without touching the live databaserypted daily artifacts)

## Key Dependencies

- [[../cli/cli.ava.okf.md]] — `ava start` owns cluster identity and service readiness; `_converge.py` applies host wiring and plugin scaffolds.
- [[../tests/tests.ava.okf.md]] — many lint scripts have corresponding `tests/test_lint_*.py`

## Notes

- pre-commit runs lints / codegen; pre-push runs pyright / tsc / eslint / vitest. Full pytest / migration smoke stay in CI; local tests are targeted.
- `start_agent.py` derives an agent via gateway `/api/agents`, respecting the ordering constraint "start gateway before agent"; `start_gateway.py` directly starts the gateway body (not agent derivation)
