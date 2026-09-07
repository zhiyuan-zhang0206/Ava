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

`scripts/` houses Ava's engineering and ops scripts — both Python (`.py`) and Shell (`.sh`), invoked by developers, pre-commit / pre-push hooks, CI pipelines, and the `ava` CLI internally. They do not participate in agent runtime; they are infrastructure around the repo for building, validating, installing, and releasing.

## Script Categories

### Lint (18 `lint_*.py` + `check_doc_references.py`)
The full linter inventory — what each one enforces and where it runs: [[scripts/lint-scripts.ava.okf.md]].

### OKF Graph Toolchain
- `build_okf_data.py` — bundle → `graph_data.json` (nodes / tree edges / cross edges)
- `serve_okf_viz.py` — renders interactive graph locally
- `fix_okf.py`, `migrate_okf.py`, `fix_frontmatter.py` — batch fix / migrate

### Installation and Provisioning
- `install.sh` — unit scaffolding, two mutually exclusive paths: `--role gateway,agent-runner|gateway|agent-runner` (prod/host install, final step runs `cli.install_cluster` to birth the cluster — registry records + own pg/redis + create db + write `$AVA_HOME/.env`, `ava start` remains the sole bring-up); `--worktree [--path P] [--no-seed]` (dev worktree self-cluster, first runs `guard_editable_venv.py`, skips brew/apt, install-dir guard, `~/.local/bin` symlinks; identity = checkout path, home defaults to `~/.ava-<checkout dir name>`, `--path` is the sole identity override — no name flag)
- `guard_editable_venv.py` — dependency-free worktree preflight that refuses symlinked/external virtualenvs and cross-checkout editable-install records before a sync can mutate them
- `install-cli-tools.sh`, `setup-worktree.sh`, `worktree.sh`
- `provision/` — `database.sh` / `node.sh` / `toolchain.sh` / `install-playwright.sh` (the Dockerfile's eval-image layer) / `install-system.sh` (Linux Debian/Ubuntu system-level install for a bare host: Python 3.12 + build tools, then composes the other scripts) / `_lib.sh`. Consumers: `install.sh`, the `Dockerfile`, `install-cli-tools.sh`.
- `install.sh --mirror cn` — select Python/npm/brew mirrors and persist the unit profile. Python install/update share `cli/python_install.py`: canonical lock validation, host-index transport with locked hashes, and the real editable checkout; existing machine uv/pip settings are reused without rewriting the lock. See [[cli/python-install.ava.okf.md]].

### CI / Release / Migration
- `qa_gate.py` + `qa_receipt.py` — evaluates exact-head QA evidence from GitHub; synthetic queue exemptions require verified Trunk identity, same-repository draft targeting main, and a complete normal or `-bisection` test ref (see [receipt contract](../conventions/qa-approval-receipt.md))
- `ci_utils.py` — polls PR CI status + merge conflict detection; separates workflow-produced checks from GitHub App ones so a suite that never ran cannot read as green (`NO_WORKFLOW_RUNS`), and submits green, labeled PRs through Trunk with a head-update cooldown, one retry, and terminal-state polling
- `coverage_gates.py` — backend CI coverage gates: the combined 85% line-rate over the core domains (agent/ava/cli/gateway/shared/ui) plus per-risk-domain minimum line floors for ops/services/ava_builtins, read from the combined shard `coverage.json`
- `audit_branch_protection.py` — read-only comparison of live GitHub branch protection / workflow activation against the required checks declared in `.trunk/trunk.yaml`; exit status distinguishes verified drift from an API/tool failure
- `build_app_update_manifest.py` — turns signed Tauri desktop archives into the app's static `latest.json`; unsigned release runs get an empty platform map
- `update_model_pricing.py` + `plugin_price_sync.py` — fail-closed official price checks and conservative archive-to-plugin synchronization of complete period/tier/window declarations for reviewed pricing PRs
- `refresh_test_durations.py` — records isolated CI-shaped pytest timing shards, retries
  only a failed measurement, and atomically merges a complete artifact set into
  `.test_durations`
- `test_selector.py` — stdlib-only, read-only PR selector that reverse-maps
  static direct test imports, preserves conservative full-suite escapes, and
  emits the shadow-run JSON consumed by `ci.yml`
- `release_cut.py`, `pre-push-check.sh`, `check_cross_branch_migrations.py`, `migration_smoke.py`, `test_migrations_apply.sh`, `test_uv_sync_write_window.sh`
- `post_deploy_visual_check.py` — read-only five-surface production visual gate: a gateway `started_at` change distinguishes deployment waves from daily sentinels, a pinned Playwright Docker image captures desktop/narrow light/dark combinations after an explicit settle predicate, shared structural probes fail P0, and stable two-frame pixel drift on static crops is attributed to the golden-to-wave frontend diff. It writes artifacts and exit codes only; the invoking agent owns notifications. Golden updates require an audited `--accept-wave`.
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

- [[../cli/cli.ava.okf.md]] — `_converge.py` **re-implements** host wiring in Python, **does not call** `install.sh` (replace/absorb relationship, not reuse); the one calling provision scripts is `install.sh` itself
- [[../tests/tests.ava.okf.md]] — many lint scripts have corresponding `tests/test_lint_*.py`

## Notes

- pre-commit runs lint scripts + ruff / ruff-format, pyright, frontend tsc / eslint / **full vitest**; heavy tasks pytest / migration smoke are left to CI
- `start_agent.py` derives an agent via gateway `/api/agents`, respecting the ordering constraint "start gateway before agent"; `start_gateway.py` directly starts the gateway body (not agent derivation)
