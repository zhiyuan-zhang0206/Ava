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

`scripts/` contains Python and shell engineering tools invoked by developers, git hooks, CI, and the CLI for installation, validation, and release. Purpose subdirectories and their own children each obey the 20-direct-entry structure budget; every nested directory counts as one direct entry in its parent.

## Script Categories

### Lint (`lint_*.py` + `check_*.py`)
Split by kind: code/AST/Python-convention guards in [[scripts/lint/docs/lint.ava.okf.md]]; document/OKF/skill/migration-format guards in [[scripts/content_lint/docs/content_lint.ava.okf.md]].

### `audit/` — read-only drift audits
`branch_protection.py` (live GitHub branch protection vs. `.trunk/trunk.yaml`), `where_used.py` + `where_used_scan.py` (before a change: every importer, test, string target, doc and baseline entry of a symbol, module or path, grouped; `--json` for scripts), `module_moves.py` (after a move: no reference to the old path may remain).

### `codegen/` — derived-artifact generation and OKF tooling
`build_okf_data.py` (bundle → `graph_data.json`), `serve_okf_viz.py` (local viewer, `okf-d3-template.html` — also read by `gateway/inspect/okf_graph.py`'s `/api/okf/graph`) — OKF tooling. `build_app_update_manifest.py` (Tauri archives → `latest.json`), `dump_event_fixtures.py`, `dump_frontend_constants.py`, `dump_openapi.py`, `gen_config_lite_table.py`, `gen_event_registry.py`, `gen_pyright_test_environments.py` (the `executionEnvironments` entries of `pyproject.toml` that hold each package's `tests/` directory to the tests type-checking standard, written between its GENERATED markers) — one generator per artifact, each with a matching `check-*-fresh.sh` or pre-commit drift gate.

### `ci/` — CI job / test infrastructure
`pull_requests/` holds `accounting.py`, `job_rerun.py`, `runs_export.py` — CI-minute attribution and job/workflow tooling; `status.py` owns reusable GitHub CI verdicts; `monitor.py` queries and waits without queue actions; `commands.py` routes the existing CLI; `owner_operations.py` owns optional authenticated queue submission/eviction and GitHub job re-runs. `scripts/ci/cli.py` preserves the CLI and the public `check_ci`, `CIResult`, `CIStatus` imports. Pending or missing workflow execution remains non-green, including aged queued runs with zero jobs; there is no status override. `pull_requests/trunk_api.py` — the Trunk merge-queue HTTP calls and PR base-freshness read shared by owner tools and `pr_flow_export.py`; `pull_requests/diagnose.py` — the `scripts/ci/cli.py --diagnose` failure classifier; `coverage_gates.py` — backend coverage gates; `test_selector.py` — static-import PR test selection feeding `ci.yml`; `refresh_test_durations.py` — `.test_durations` refresh; `shard_counts.py` — the executed-test counts (per shard and directory, and the total against the previous main run) that the backend shards and the `backend test counts (all shards)` job print, plus the root leak guard's findings from the JUnit properties (one annotation and an artifact; a shard that left no JUnit report or ran no test fails there: [[../../.github/test-gate.ava.okf.md]]); `migration_smoke.py`, `pgvector_runtime_smoke.py`, `two_section_chain_smoke/` — smoke/verification gates.

### `verify/` — the verification boundaries (Linux container, Tart macOS VM)
Host side, stdlib only: `boundary.py` (what both recipes share: the start profile, the `ava init` and `ava start` argv, the refusal to let host state in, the commit resolution, the evidence directory with its bounded, logged steps), `container.py` (builds the image when its inputs changed, starts one container per commit, clones the commit into `~/.ava/source`, runs `ava init` and the first `ava start`, copies the evidence out, removes the container) over `Dockerfile` (the image: the repository's own provisioning scripts, an ordinary user, no source or secret), and `tart_run.py` (clones the golden Tart image into a throwaway macOS VM, fetches the commit from a read-only export, provisions the toolchain, runs `ava init`, retires the golden image's loaded helper with guest-local `ava stop` before the first `ava start`, observes, copies the evidence out, deletes the VM) over `tart_vm.py` (the Tart driver: the two-VM ceiling, the one read-only share, deletion limited to run VMs) and `tart_golden.py` (builds the golden image once, with a person for the two desktop grants). `observe.py` runs inside either boundary: toolchain, service identity probes, frontend, CORS and a scripted agent executing `print(1 + 2)`, plus on macOS the signed helper chain (launchd, helper, root, every unit) and its desktop grants. Design and claims: [verification boundaries](../../future/infra/engineering/verification-boundaries.md).

### Startup / Deployment / Multi-host
- `entrypoints/agent.py` (spawns a root agent through gateway `POST /api/agents`; the gateway must already be up), `entrypoints/gateway.py` (directly starts the gateway FastAPI body, ≈ `.venv/bin/python -m gateway`) — **the latter does not spawn an agent**
- `data_plane_ops/rotate_cluster_secret.py` — the human-bearer `AVA_CLUSTER_SECRET` rotation on the gateway (`advance`): journaled as fingerprints, it verifies the pinned logical-backup passphrase (pinning `sha256(secret)` only on a home without a pin) before writing the new secret; default dry run (`--execute` performs it); it does not change the data plane
- `data_plane_ops/rotate_data_plane_secrets.py` — gateway-host rotation of the Redis credentials (`--scope admin` for the `requirepass` password, `runner` for the runtime ACL password, `both` by default); default dry run (`--execute` performs it), resumable from a 0600 state file; PostgreSQL has no rotatable password here
- `data_plane_ops/restore_drill.py` — decrypts a managed database backup, restores it into throwaway Postgres, and verifies schema, table counts, and a readable checkpoint conversation without touching the live database; scratch files and the throwaway cluster are removed afterwards

## Key Dependencies

- [[../../cli/docs/cli.ava.okf.md]] — `ava start` owns cluster identity and service readiness; `converge/host.py` applies host wiring and plugin scaffolds.
- [[../../tests/docs/tests.ava.okf.md]] — many lint scripts have corresponding `tests/test_lint_*.py`

## Notes

- `hooks/prepush-base.sh` owns the contribution range for branch lint, pyright, input gating, artifact freshness and `provision/prepush_frontend.py`; missing base fails. Frontend selection includes known filesystem consumers and reports global/deletion closure as CI-only.
- pre-commit runs lints / codegen; pre-push runs pyright / tsc / eslint / vitest. Full pytest / migration smoke stay in CI; local tests are targeted.
