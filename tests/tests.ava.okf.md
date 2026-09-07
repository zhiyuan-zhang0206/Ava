---
type: doc
title: "Test Suite"
description: "`tests/` is Ava's traditional pytest test suite, covering all modules agent / gateway / cli / shared."
tags:
- evaluation
- tool
- quality-assurance
---

# Test Suite

## What it is

`tests/` is Ava's traditional pytest test suite, covering all modules agent / gateway / cli / shared.

## Core responsibilities

### Test layers

| Layer | Location | Description |
|---|---|---|
| **Unit tests** | `tests/{module}/test_{file}.py` | one test file per source file |
| **Integration tests** | `tests/integration/` | cross-module tests; `TestClient` mounts `gateway.app` in-process + custom `_TestClientTransport` forwarding httpx, **no separate Gateway process** |
| **E2E tests** | `tests/e2e/` | full-stack end-to-end tests |
| **Data factories** | `tests/factories/` | test data construction tools |

`tests/e2e/_layout_assertions.py` is the shared real-browser structural layer for
document overflow, viewport containment, center-point occlusion, nonempty blocks,
and settle-before-capture. Both the layout-invariant suite and the post-deploy
visual gate consume it so their definitions cannot drift.

### Test coverage scope
- `tests/agent/` — agent core (loop, graph, messages, state, hooks)
- `tests/ava/` — SDK surface (ava.* namespace)
- `tests/gateway/` — API gateway
- `tests/cli/` — command-line tools
- `tests/shared/` — shared library
- `tests/services/` — backend services
- `tests/ops/` — ops layer (18 files: controllers pin/schema/stranded-pause/code, updater outcomes + stall reap, manager, heal records, deploy window, spec roster, plugin services, spawn birth config, cluster ops)
- `tests/skills/` — skills
- `tests/scripts/` — release/docs/secret-rotation scripts plus the shell updater-manifest builder; lint script tests at `tests/` root `test_lint_*.py`
- `tests/ui/` — deterministic tests for applying the shell's Kotlin/XML/signing overlay to Tauri's generated Android project
- `tests/plugins/` — plugin tests (`test_ava_memory_lint.py` / `test_ava_memory_notes.py` for the ava_memory plugin, `ava_fleet/` subtree)
- `tests/fixtures/` + `tests/factories/` — test fixture data and data factories

- `tests/scripts/test_test_selector.py` — synthetic-checkout contracts for
  the static PR test selector, including queue, blind-file, duration, and
  process-determinism escapes
- `tests/scripts/test_ci_test_selection.py` — workflow contracts keeping shadow
  selection non-gating and comparing the matching non-flaky pytest population

### Global fixture (`conftest.py`, ~70KB)
- **Per xdist worker / session** a pair of throwaway pg/redis + per-session databases; per-test isolation via autouse TRUNCATE + checkpoint re-setup (**not** a full instance per test)
- A killed run (Ctrl-C, SIGKILL, an agent dying mid-run) leaks its throwaway **Postgres**, because the detached postmaster outlives an owner that ran no finalizer. It is bounded not by teardown but by a **sweep at the start of the next spin-up**: `shared.pg_tools.sweep_orphaned_throwaway_clusters` reaps the instances whose owner is provably gone, proof being an exclusive `flock` the owner held for the instance's whole life on an `owner.lock` inside that instance's own dir (so the proof shares the cluster's exact lifetime, and two UNIX users on one `/dev/shm` never contend for a shared registry). The throwaway **redis** leaks the same way and is not swept (a redis orphan costs RAM, not the System V segment that wedges the box)
- Standalone `conftest.py` only in 6 subdirectories: `agent` / `ava` / `cli` / `gateway` / `integration` / `e2e` (**not** "each module")
- **The env block at the top of `conftest.py` must stay above every project import.** `shared.dotenv_boot` resolves the home once at import and binds `AVA_ENV_PATH` from it, so AVA_HOME set after that import has no effect on it — the suite then loads the operator's real `~/.ava/.env`, and `_enforce_cluster_env_authority()` force-assigns the production cluster secret / db / redis / gateway URL over the sentinels conftest just set. `_assert_env_precedes_project_imports()` fails the run if a project module was imported early; `tests/test_home_isolation.py` asserts the outcome independently of mechanism
- A family of autouse **host-resource guards** makes "don't touch the host" the default rather than something each test author remembers: agent launch, daemon respawn, cluster spawn, OS cron, warm-up, and `os.exec*`. Each is overridable inside a test body (or via a `real_*` marker) when the guarded call IS the subject under test. The `os.exec*` one is the odd member — it guards the *test runner itself*, since an exec replaces the pytest process and the run ends with no summary and no failure report

### CI integration
- `.github/workflows/` — GitHub Actions runs the full suite automatically
- pre-commit runs ruff / ruff-format, pyright, frontend tsc, eslint, **full frontend vitest** in addition to lint (`.pre-commit-config.yaml`); just doesn't run pytest
- CI runs all non-e2e tests + e2e + coverage thresholds
- Every pre-commit hook runs in CI too, so no lint is local-only: the `backend` job runs `pre-commit run --all-files` (skipping the four npx hooks the `frontend` job covers), and the `docs-only` job — which the `changes` path filter routes markdown-only PRs to, skipping `backend` entirely — runs the same command so the doc lints (OKF size/format, doc symbol + roster sync, AGENTS.md / SKILL.md ceilings) still gate exactly the PRs that can break them

## Key dependencies

- [[db.ava.okf.md]] — tests use isolated Postgres/Redis
- [[loop.ava.okf.md]] — system under test
- [[gateway-cli.ava.okf.md]] — integration tests mount `gateway.app` in-process (TestClient), no separate Gateway process needed

## Entry points

- `.venv/bin/pytest tests/ --ignore=tests/e2e -q` — run all non-e2e tests
- `.venv/bin/pytest tests/agent/ -q` — run a single module
- `.venv/bin/pytest tests/ --cov=agent --cov=ava ...` — with coverage report

## Notes

- **Test strategy**: commit fast + CI full. Locally only run relevant tests, CI is the merge gate
- **Isolation**: each test gets isolated DB/Redis, avoiding parallel conflicts
- **Do not** run the full suite locally — Mac mini resources are limited (conftest header docstring specifically designed for concurrency isolation: per-session database names + random free ports + Redis channel suffixes, **concurrent sessions will not conflict**, just resource-intensive)
- `tests/test_agent_error_wire_equivalence.py` parametrized verification of agent ↔ gateway error wire protocol consistency
