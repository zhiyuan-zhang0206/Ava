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
- `lint_python_lock.py` — wraps packaged `shared/python_lock.py`: `uv.lock` requires PyPI registry and `files.pythonhosted.org` distribution URLs; regional mirrors stay host-local. Pre-commit + always-on `repo-language` CI; no project dependencies needed.
- `lint_no_tailnet.py` — bans 100.64.0.0/10 host literals repo-wide; allows CIDR notation, `decisions/`, and `# tailnet-ip-ok:` boundary tests. Pre-commit `lint-no-tailnet` + always-on `repo-language` CI (2026-08-03/04 Gateway-URL and 2026-08-20 public-repo rulings).
- `lint_fail_fast.py`, `lint_no_emoji.py`, `lint_no_os_environ.py`, `lint_no_script_sibling_imports.py` — Python conventions; the last requires script-mode sibling imports to restore their directory to sys.path under PYTHONSAFEPATH=1 (2026-08-23 `daily_scan.py` crash).
- `lint_code_structure.py` + `structure/quality_budget.py` — 800-line/20-entry caps and function CC ≥15 / nesting >5 violations (packages/tests/scripts). `structure/baseline.json` freezes `directories`, `files`, `complexity`, `nesting`; its base-ref guard allows shrinkage, same-file function rename pairing, and git-detected file renames (migrate keys with the move). CC 10–14 warns per file; `--complexity-warnings-full` unfolds all warning counts.
- `lint_termination_source.py` — every `UPDATE agents_meta SET status='terminated'` must stamp `termination_source` in the same statement (AST catches bind parameters too). A NULL source is permanently unresurrectable and strands queued work.
- `lint_clock_lattice.py` — lattice-vocabulary timing constants (STALL / GRACE / REAP / BUDGET / WEDGED / NO_PROGRESS / LOCK_TTL / UPDATER_LEASE / SETTLE_TTL / LAUNCH_CONFIRM / LEASE_TTL / LEASE_RENEW / SCAN_INTERVAL) may only be defined in the clock-lattice family modules (`shared/timing.py` / `boot_timing.py` / `deploy_timing.py` / `stop_timing.py` / `schedule_timing.py` / `cluster_lock.py` / `host_deploy_state.py`), as aliases of a registered clock, or with an explicit stated exemption — a bare `_SOME_REAP_GRACE_S` outside the lattice is the 2026-07-30 spawn incident's seedling; the lattice topology itself lives in `shared/timing.py`
- `lint_time_bomb.py` — tests may not exactly assert a value derived from a repo fixed-instant constant (`datetime(2026, …)`) when the derivation can reach the real clock (unpinned `now=`/`at=`, or an opaque `client.get(...)`); pin the clock, use a tolerance, or opt out with `# time-bomb-ok:` — the 2026-08-30 pair of deterministic-red long-window tests (agent-inspect, events-rollup). Source half: a function accepting a clock parameter must thread it into fixed-instant window boundaries (the 2026-08-30 rollup bomb: `compute_rollup(now_utc=...)` reaching `split_index_label_window` without `now=`; that seam has since been removed). Fixture half: a fixed calendar literal (`"2026-09-06"`, `date(2026, 6, 9)`, `datetime(2026, 7, 22, 18, tzinfo=UTC)`) bound to a window-shaped name (`day`, `date`, `since`, `until`, `window_start`, `window_end`) as a dict value, keyword argument, or plain assignment must derive from the clock or carry the marker — the 2026-09-13 queue-level red
- `lint_pool_keepalives.py` — every psycopg pool must carry `PG_KEEPALIVE_KWARGS` (AST-based, so it sees through `AsyncConnectionPool[T](...)` subscripts and `LoggingConnectionPool` subclasses). Sync pools get it by calling `shared.db.pool()`; the async pools that have no factory unpack the constant. Pool connections are long-lived, so a missing keepalive is invisible until a woken-from-sleep borrow stalls minutes on the OS TCP-retransmit timeout
- `lint_fixture_scope.py` — a pytest fixture may not mutate a process global at a scope that outlives its blast radius. Two rules, both AST: (1) `scope="session"` outside the root `tests/conftest.py` plus any write to `os.environ` / a `settings` field / a module global — its teardown fires at end-of-session, not on leaving the fixture's own directory, so every test collected after that directory runs with the mutated value (this is how `tests/e2e/`'s env layering disarmed `tests/test_home_isolation.py` on `main` while CI stayed green); (2) `scope="package"` in a directory with no `__init__.py`, where `_pytest.fixtures.get_scope_package` finds no `Package` node and silently returns the SESSION node — the keyword reads correctly and does nothing. `tests/conftest.py` is the only exemption (its session scope IS its blast radius); a session fixture that owns only an expensive resource and hands it back through the return value is not flagged
- `lint_agent_docstrings.py`, `lint_agents_md_size.py`, `lint_doc_roster.py`, `lint_doc_symbols.py` (`ava.*` refs), `lint_doc_anchors.py` (code anchors, resolved against the AST), `lint_skill_descriptions.py`, `lint_skill_md_size.py` — document / SDK / skill guards
- `lint_note_tags.py` — bidirectional NoteTag / timeline-marker contract: every backend tag has a frontend dispatch branch and every lifecycle, memory, or note dispatch member is a live backend tag
- `lint_no_plugin_wrap.py` — plugins may not bare monkey-patch `ava.*` (must go through `ava.extend.wrap`); wired into pre-commit
- `check_doc_references.py` — validates every CLI flag in the docs against the argparse tree and `scripts/*.sh` case branches, plus relative markdown links; runs on every pre-commit commit (`pass_filenames: false`) and in the always-on `doc-lints` CI job (the classify-independent doc-lint family, so docs-only PRs are covered) (`lint_skill_md_size.py`: hard cap of 300 lines / soft zone 250-300 for SKILL.md, pushing progressive disclosure — root SKILL.md as index, depth sinks into sub-skills)
- `lint_migrations.py` — timestamp-id + applied-set scheme checks: filename format, unique names, up/down `.down.sql` pairing, `db/schema.sql` baseline/folded-migration stamping, and a later-drop plan for every `*_backfill_*` snapshot table; **no expand-contract check** (that's a documentation discipline, not lint)

## CLI contract (explicit targets)

The `lint_*.py` gates that take explicit path arguments share one contract — a
typo'd target must never pass as a silent empty scan, and an out-of-repo target
must scan rather than crash:

- **No arguments** — scan the script's default scope (its `_SCAN_DIRS`, the
  git-tracked file list, ...).
- **Explicit arguments must resolve to an existing path.** Any argument that
  does not is a hard error: `error: target path(s) not found: <argument(s)>` on
  stderr, exit 1. Resolution is per-script: absolute paths are used as-is; a
  relative path is resolved against the repo root first with a caller-cwd
  fallback (`lint_no_cjk` / `lint_no_tailnet`), against the repo root only
  (`lint_time_bomb`), or against the caller's cwd (the other scripts; pre-commit
  passes absolute paths).
- **Out-of-repo targets are scanned.** An existing target outside the repository
  is scanned under its absolute path. Scope-anchored scripts
  (`lint_code_structure` / `lint_fixture_scope` / `lint_no_plugin_wrap`) keep
  their own scope filter: a target or member outside it is skipped silently
  (rc 0). Directory targets enumerate their members
  (`lint_turn_scoped_config` takes `.py` files only: a directory argument scans
  nothing); an unreadable member (a dangling `*.py` symlink, non-UTF-8 content)
  is skipped like any unreadable file. One index caveat: `lint_time_bomb`
  resolves callees through its repo-scoped index, so an out-of-repo source
  file's source half silently passes (rc 0) — only its test half, which reads
  the target directly, applies.

Parent: [[scripts/scripts.ava.okf.md|scripts]].
