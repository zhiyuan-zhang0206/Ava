---
type: doc
title: Lint Scripts
description: The lint_*.py code/document structure guards plus check_doc_references.py — what each one enforces and where it runs (pre-commit / CI).
tags:
- scripts
- lint
---

# Lint Scripts

## The linters

Code and document structure guards, mostly invoked by `.pre-commit-config.yaml` and CI:
- `lint_ava_okf.py` — OKF format validation (frontmatter / size / wikilink)
- `lint_fail_fast.py`, `lint_no_emoji.py`, `lint_no_os_environ.py`, `lint_code_structure.py` — Python conventions
- `lint_termination_source.py` — every `UPDATE agents_meta SET status='terminated'` must stamp `termination_source` in the same statement (AST-based, so it catches the bind-parameter form a grep misses); a NULL source is permanently unresurrectable, so a forgotten stamp silently strands the agent's queued work
- `lint_clock_lattice.py` — lattice-vocabulary timing constants (STALL / GRACE / REAP / BUDGET / WEDGED / NO_PROGRESS / LOCK_TTL / UPDATER_LEASE / SETTLE_TTL / LAUNCH_CONFIRM / LEASE_TTL / LEASE_RENEW / SCAN_INTERVAL) may only be defined in the clock-lattice family modules (`shared/timing.py` / `boot_timing.py` / `deploy_timing.py` / `cluster_lock.py` / `host_deploy_state.py`), as aliases of a registered clock, or with an explicit stated exemption — a bare `_SOME_REAP_GRACE_S` outside the lattice is the 2026-07-30 spawn incident's seedling; the lattice topology itself lives in `shared/timing.py`
- `lint_pool_keepalives.py` — every psycopg pool must carry `PG_KEEPALIVE_KWARGS` (AST-based, so it sees through `AsyncConnectionPool[T](...)` subscripts and `LoggingConnectionPool` subclasses). Sync pools get it by calling `shared.db.pool()`; the async pools that have no factory unpack the constant. Pool connections are long-lived, so a missing keepalive is invisible until a woken-from-sleep borrow stalls minutes on the OS TCP-retransmit timeout
- `lint_fixture_scope.py` — a pytest fixture may not mutate a process global at a scope that outlives its blast radius. Two rules, both AST: (1) `scope="session"` outside the root `tests/conftest.py` plus any write to `os.environ` / a `settings` field / a module global — its teardown fires at end-of-session, not on leaving the fixture's own directory, so every test collected after that directory runs with the mutated value (this is how `tests/e2e/`'s env layering disarmed `tests/test_home_isolation.py` on `main` while CI stayed green); (2) `scope="package"` in a directory with no `__init__.py`, where `_pytest.fixtures.get_scope_package` finds no `Package` node and silently returns the SESSION node — the keyword reads correctly and does nothing. `tests/conftest.py` is the only exemption (its session scope IS its blast radius); a session fixture that owns only an expensive resource and hands it back through the return value is not flagged
- `lint_agent_docstrings.py`, `lint_agents_md_size.py`, `lint_doc_roster.py`, `lint_doc_symbols.py` (`ava.*` refs), `lint_doc_anchors.py` (code anchors, resolved against the AST), `lint_skill_descriptions.py`, `lint_skill_md_size.py`, `lint_note_tags.py` — document / SDK / skill guards
- `lint_no_plugin_wrap.py` — plugins may not bare monkey-patch `ava.*` (must go through `ava.extend.wrap`); wired into pre-commit
- `check_doc_references.py` — validates every CLI flag in the docs against the argparse tree and `scripts/*.sh` case branches, plus relative markdown links; runs on every pre-commit commit (`pass_filenames: false`) (`lint_skill_md_size.py`: hard cap of 300 lines / soft zone 250-300 for SKILL.md, pushing progressive disclosure — root SKILL.md as index, depth sinks into sub-skills)
- `lint_migrations.py` — timestamp-id + applied-set scheme checks: filename format, unique names, up/down `.down.sql` pairing, and `db/schema.sql` baseline stamping; **no expand-contract check** (that's a documentation discipline, not lint)

Parent: [[scripts/scripts.ava.okf.md|scripts]].
