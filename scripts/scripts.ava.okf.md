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
Code and document structure guards, mostly invoked by `.pre-commit-config.yaml` and CI:
- `lint_ava_okf.py` — OKF format validation (frontmatter / size / wikilink)
- `lint_fail_fast.py`, `lint_no_emoji.py`, `lint_no_os_environ.py`, `lint_code_structure.py` — Python conventions
- `lint_termination_source.py` — every `UPDATE agents_meta SET status='terminated'` must stamp `termination_source` in the same statement (AST-based, so it catches the bind-parameter form a grep misses); a NULL source is permanently unresurrectable, so a forgotten stamp silently strands the agent's queued work
- `lint_clock_lattice.py` — lattice-vocabulary timing constants (STALL / GRACE / REAP / BUDGET / WEDGED / NO_PROGRESS / LOCK_TTL / UPDATER_LEASE / SETTLE_TTL / LAUNCH_CONFIRM / LEASE_TTL / LEASE_RENEW / SCAN_INTERVAL) may only be defined in the clock-lattice family modules (`shared/timing.py` / `boot_timing.py` / `deploy_timing.py` / `cluster_lock.py` / `host_deploy_state.py`), as aliases of a registered clock, or with an explicit stated exemption — a bare `_SOME_REAP_GRACE_S` outside the lattice is the 2026-07-30 spawn incident's seedling; the lattice topology itself lives in `shared/timing.py`
- `lint_pool_keepalives.py` — every psycopg pool must carry `PG_KEEPALIVE_KWARGS` (AST-based, so it sees through `AsyncConnectionPool[T](...)` subscripts and `LoggingConnectionPool` subclasses). Sync pools get it by calling `shared.db.pool()`; the async pools that have no factory unpack the constant. Pool connections are long-lived, so a missing keepalive is invisible until a woken-from-sleep borrow stalls minutes on the OS TCP-retransmit timeout
- `lint_fixture_scope.py` — a pytest fixture may not mutate a process global at a scope that outlives its blast radius. Two rules, both AST: (1) `scope="session"` outside the root `tests/conftest.py` plus any write to `os.environ` / a `settings` field / a module global — its teardown fires at end-of-session, not on leaving the fixture's own directory, so every test collected after that directory runs with the mutated value (this is how `tests/e2e/`'s env layering disarmed `tests/test_home_isolation.py` on `main` while CI stayed green); (2) `scope="package"` in a directory with no `__init__.py`, where `_pytest.fixtures.get_scope_package` finds no `Package` node and silently returns the SESSION node — the keyword reads correctly and does nothing. `tests/conftest.py` is the only exemption (its session scope IS its blast radius); a session fixture that owns only an expensive resource and hands it back through the return value is not flagged
- `lint_agent_docstrings.py`, `lint_agents_md_size.py`, `lint_doc_roster.py`, `lint_doc_symbols.py`, `lint_skill_descriptions.py`, `lint_skill_md_size.py`, `lint_note_tags.py` — document / SDK / skill guards
- `lint_no_plugin_wrap.py` — plugins may not bare monkey-patch `ava.*` (must go through `ava.extend.wrap`); wired into pre-commit
- `lint_trace_anchors.py` — validates every `file:symbol` anchor in `traces/` still resolves
- `check_doc_references.py` — validates every CLI flag in the docs against the argparse tree and `scripts/*.sh` case branches, plus relative markdown links; runs on every pre-commit commit (`pass_filenames: false`) (`lint_skill_md_size.py`: hard cap of 300 lines / soft zone 250-300 for SKILL.md, pushing progressive disclosure — root SKILL.md as index, depth sinks into sub-skills)
- `lint_migrations.py` — timestamp-id + applied-set scheme checks: filename format, unique names, up/down `.down.sql` pairing, and `db/schema.sql` baseline stamping; **no expand-contract check** (that's a documentation discipline, not lint)

### OKF Graph Toolchain
- `build_okf_data.py` — bundle → `graph_data.json` (nodes / tree edges / cross edges)
- `serve_okf_viz.py` — renders interactive graph locally
- `fix_okf.py`, `migrate_okf.py`, `fix_frontmatter.py` — batch fix / migrate

### Installation and Provisioning
- `install.sh` — unit scaffolding, two mutually exclusive paths: `--role gateway,agent-runner|gateway|agent-runner` (prod/host install, final step runs `cli.install_cluster` to birth the cluster — registry records + own pg/redis + create db + write `$AVA_HOME/.env`, `ava start` remains the sole bring-up); `--worktree [--path P] [--no-seed]` (dev worktree self-cluster, skips brew/apt, install-dir guard, `~/.local/bin` symlinks; identity = checkout path, home defaults to `~/.ava-<checkout dir name>`, `--path` is the sole identity override — no name flag)
- `install-cli-tools.sh`, `setup-worktree.sh`, `worktree.sh`
- `provision/` — `database.sh` / `node.sh` / `toolchain.sh` / `install-playwright.sh` (the Dockerfile's eval-image layer) / `install-system.sh` (Linux Debian/Ubuntu system-level install for a bare host: Python 3.12 + build tools, then composes the other scripts) / `_lib.sh`. Consumers: `install.sh`, the `Dockerfile`, `install-cli-tools.sh`.
- `install.sh --mirror cn` — route pip/npm/brew through CN mirrors (loads `mirrors/cn.env`, copies mirror config to `~/.ava/mirror.env` for every subsequent `ava` command); `--warm-mcp` pre-warms the MCP server

### CI / Release / Migration
- `ci_utils.py` — polls PR CI status + merge conflict detection; separates workflow-produced checks from GitHub App ones so a suite that never ran cannot read as green (`NO_WORKFLOW_RUNS`)
- `build_shell_update_manifest.py` — turns signed Tauri desktop archives into the shell's static `latest.json`; unsigned release runs get an empty platform map
- `update_model_pricing.py` — fail-closed official price checks that propose reviewed catalog PRs
- `release_cut.py`, `pre-push-check.sh`, `check_cross_branch_migrations.py`, `migration_smoke.py`, `test_migrations_apply.sh`

### Code Generation
- `codegen-types.sh`, `check-types-fresh.sh`, `dump_openapi.py`, `generate-ui-page.py`, `dump_event_fixtures.py`

### Startup / Deployment / Multi-host
- `start_agent.py` (derives an agent via gateway `/api/agents`), `start_gateway.py` (directly starts the gateway FastAPI body, ≈ `.venv/bin/python -m gateway`) — **the latter does not derive an agent**
- `multihost/` (`multihost.py` + `agent_runner_entrypoint.sh`), `preview/` (daily deployment + checkpoint rebuild)
- `cloud-bench-bootstrap.sh`, `metrics.py`
- `rotate_cluster_secret.py` — end-to-end `AVA_CLUSTER_SECRET` rotation on the gateway machine (default `--dry-run`), reusing `shared.cluster` ensure primitives (cluster role / redis ACL / pgbouncer)

## Key Dependencies

- [[../cli/cli.ava.okf.md]] — `_converge.py` **re-implements** host wiring in Python, **does not call** `install.sh` (replace/absorb relationship, not reuse); the one calling provision scripts is `install.sh` itself
- [[../tests/tests.ava.okf.md]] — many lint scripts have corresponding `tests/test_lint_*.py`

## Notes

- pre-commit runs lint scripts + ruff / ruff-format, pyright, frontend tsc / eslint / **full vitest**; heavy tasks pytest / migration smoke are left to CI
- `start_agent.py` derives an agent via gateway `/api/agents`, respecting the ordering constraint "start gateway before agent"; `start_gateway.py` directly starts the gateway body (not agent derivation)
