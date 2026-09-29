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

`scripts/` contains Python and shell engineering tools invoked by developers, git hooks, CI, and the CLI for installation, validation, and release. Grouped into purpose subdirectories to hold the 20-direct-entry structure budget (a subdirectory counts as one entry regardless of its own size); a handful of scripts stay at `scripts/` root because something outside this directory calls them by their exact path (see below).

## Script Categories

### Lint (`lint_*.py` + `check_*.py`)
Split by kind: code/AST/Python-convention guards in [[scripts/lint/lint.ava.okf.md]]; document/OKF/skill/migration-format guards in [[scripts/content_lint/content_lint.ava.okf.md]].

### `audit/` — read-only drift audits
`branch_protection.py` (live GitHub branch protection vs. `.trunk/trunk.yaml`), `module_moves.py`, `split_reexports.py` (package-door split/re-export shape checks, also called by the structure gate).

### `codegen/` — derived-artifact generation and OKF tooling
`build_okf_data.py` (bundle → `graph_data.json`), `serve_okf_viz.py` (local viewer, `okf-d3-template.html` — also read by `gateway/routers/okf_graph.py`'s `/api/okf/graph`), `fix_okf.py`, `migrate_okf.py`, `fix_frontmatter.py` — OKF tooling. `build_app_update_manifest.py` (Tauri archives → `latest.json`), `build_hierarchy_once.py`, `dump_event_fixtures.py`, `dump_frontend_constants.py`, `dump_openapi.py`, `gen_config_lite_table.py`, `gen_event_registry.py`, `generate-ui-page.py` — one generator per artifact, each with a matching `check-*-fresh.sh` or pre-commit drift gate.

### `ci/` — CI job / test / release-cut infrastructure
`accounting.py`, `job_rerun.py`, `runs_export.py` — CI-minute attribution and job/workflow tooling; `coverage_gates.py` — backend coverage gates; `qa_gate.py` + `qa_receipt.py` — exact-head QA evidence evaluation (see [receipt contract](../conventions/qa-approval-receipt.md)); `test_selector.py` — static-import PR test selection feeding `ci.yml`; `refresh_test_durations.py` — `.test_durations` refresh; `migration_smoke.py`, `pgvector_runtime_smoke.py`, `two_section_chain_smoke.py`, `verify_runtime_wheel.py` — smoke/verification gates; `release_cut.py`, `tag_latest.py` — dated release tagging.

### `release_proofs/` — release-prepare / retained-image verification (CI-only)
`prepare_plugin_fixture.py` plus the `prove_runtime_*` / `prove_release_inventory.py` / `prove_exec_owner_installed.py` / `prove_native_launcher_reads.py` family: each is copied into an isolated scratch interpreter (`-I`, no checkout visible) to prove a captured release image is self-contained. Invoked from `.github/workflows/runtime-*.yml`.

### `post_deploy_visual/` — production visual regression gate
`check.py` — read-only five-surface production visual gate; a `started_at` change distinguishes deployment waves from daily sentinels, and stable two-frame pixel drift is attributed to the golden-to-wave diff (audited `--accept-wave` updates the golden). `matrix.py` (capture matrix, shared with the blocking CI preview gate `tests/e2e/test_preview_visual_gate.py`), `_browser_js.py`, `_fixtures.py`, `_policy.py`, `_runner.py`, `refresh_visual_baselines.py`. Details + cron example: `README.md` here.

### `model_registry/` — provider model / pricing sync
`check_model_updates.py` — daily comparison of official provider models against Ava's registry; `update_model_pricing.py` + `plugin_price_sync.py` — price checks and archive-to-plugin rate sync.

### `data_plane_ops/` — secret rotation and backup/restore
`rotate_cluster_secret.py` — the human-bearer `AVA_CLUSTER_SECRET` rotation on the gateway; `rotate_data_plane_secrets.py` — routine data-plane rotation; `pitr_baidu_speedtest.py`, `pitr_migrate_gcs_to_baidu.py` — PITR backend tooling; `restore_drill.py` — restores a managed backup into throwaway Postgres and verifies it (also imported at runtime by `services/backup_scheduler/worker.py`).

### `data_repair/` — one-shot production-data repair and reconciliation
`backfill_llm_usage_hourly.py` — rebuilds `llm_usage_hourly` from the frozen cold-archive extract; `fix_fork_lineage_loki.py` — corrects misrecorded fork events in Loki; `memory_search_reconcile.py` — read-only backend-switch reconciliation; `migrate_skill_identity.py` — R2-B legacy skill-identity migration.

### `host_ops/` — host/OS maintenance and diagnostic utilities
`guard_editable_venv.py` — dependency-free worktree preflight refusing symlinked/external virtualenvs; `inbound_sweep_backlog.py` — hosted-inbound dead-letter backlog sweep; `oob_triage.py` — out-of-band outage triage over SSH; `render_wsl_boot_task.py` — renders an opt-in WSL host boot task; `f5_lwcr_common.py`, `f5_lwcr_smappservice.py`, `lwcr_fault_injection.py` — F5/LWCR macOS launchd staleness experiment tooling. `tcc-onboard-helper-grants.py` stays at root instead (see below).

### Host setup, startup, and legacy fixtures
Cluster initialization belongs to `ava start` (`ava start --worktree` for an isolated development home); `provision/`, `worktree.sh`, `setup-worktree.sh`, `install-cli-tools.sh` (root-level, see below) prepare host tools or development checkouts and do not provide another init path. `cli/python_install.py` handles source dependency sync (canonical lock validation, host-index transport, the editable checkout) — see [[cli/python-install.ava.okf.md]]. `start_agent.py` derives an agent via gateway `/api/agents`; `start_gateway.py` directly starts the gateway body (≈ `.venv/bin/python -m gateway`, **not** agent derivation) — "start gateway before agent" is the ordering constraint the two together respect. `preview/` is A→B→A upgrade-cycle preview tooling; `legacy_lkg/` holds frozen last-known-good cutover fixtures.

## Scripts that stay at `scripts/` root

Not grouped into a subdirectory because an external caller, a frozen
cross-version contract, or another region's ownership pins the exact path:
`start_agent.py`, `check_worktree_remove.py`, `ci_utils.py` (all three
named by path in `AGENTS.md` / the runbook), `lint_pool_keepalives.py`
(Postgres-dial locality is a separate region), `pr_flow_export.py` (its path
is built into a registered launchd/cron job command by
`shared/host/system/pr_flow_job.py`), `prepare_otel_release.py` (`runpy.run_path`'d from
`cli/release_prepare/acquisition_assets.py`'s isolated-subprocess release
proof), the `cutover_*.py` family (runbook / FC-10 call them by path;
retired at the FC-16 cutover), and `tcc-onboard-helper-grants.py` (compiled
into the signed macOS permissions-helper panel,
`services/permissions_helper/helper/main.swift`). Also unmoved regardless:
`install.sh`, `ava-launcher.sh` (prod's `~/.local/bin/ava` symlink target),
`codegen-types.sh` (hardcoded in a pre-commit hook prompt), `provision/`,
`preview/` (README-published `python3 -m scripts.preview.local`),
`worktree.sh`, `setup-worktree.sh`, `install-cli-tools.sh`,
`prepush-guard.sh`, `test_migrations_apply.sh`, the `tcc-*.sh` drill
scripts, `prepare_frontend_release.mjs` / `prove_frontend_release.mjs`
(non-Python, exec'd by path from `cli/release_prepare/`), and
`runtime-node-version`.

## Key Dependencies

- [[../cli/cli.ava.okf.md]] — `ava start` owns cluster identity and service readiness; `converge/host.py` applies host wiring and plugin scaffolds.
- [[../tests/tests.ava.okf.md]] — many lint scripts have corresponding `tests/test_lint_*.py`

## Notes

- pre-commit runs lints / codegen; pre-push runs pyright / tsc / eslint / vitest. Full pytest / migration smoke stay in CI; local tests are targeted.
