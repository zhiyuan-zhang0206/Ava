---
type: doc
title: scripts/lint/ — Code and AST Safety Lints
description: The lint_*.py code/AST/Python-convention guards in scripts/lint/ — what each enforces, where it runs (pre-commit / CI), and the shared CLI contract for explicit-target lints. Doc/content guards split out to scripts/content_lint/.
tags:
- scripts
- lint
---

# scripts/lint/ — Code and AST Safety Lints

Code-quality and Python-convention guards, mostly invoked by
`.pre-commit-config.yaml` and CI. Document / OKF / skill / migration-format
guards are a separate group: [[scripts/content_lint/docs/content_lint.ava.okf.md]].

## The linters

- `code_structure.py` + `../../structure/budgets/` and the `../../structure/` locality/ambient helpers — strict 800-line/20-entry/CC/nesting budgets; locality (doors, owners, no `ava_builtins/` path imports; `python-conventions.md`); and [[scripts/lint/docs/ambient-state.ava.okf.md|ambient state]]. Locality and path-import sites fail directly. Only `patch_targets` and `ambient_state` retain exact, shrink-only site maps. Budgets have no exemptions or rename allowances. CC 10-14 warns; `--complexity-warnings-full` unfolds counts.
- `patch_targets.py` + `../../structure/{placement,patch_points,patch_targets,patch_report}.py` — a test may not patch a private name of a package it does not belong to (its home follows its subject's dependencies, not its directory); frozen in the `patch_targets` baseline section; `--report` prints the census. Detail: [[scripts/lint/docs/patch-targets.ava.okf.md]].
- `../../lint_pool_keepalives.py` — a psycopg pool in `scripts/` or in a module the `postgres-dial` decision allows must carry `PG_KEEPALIVE_KWARGS` (AST-based, sees through `AsyncConnectionPool[T](...)` subscripts and `LoggingConnectionPool` subclasses); elsewhere Rule 5 already routes every pool through `base.db.pool()` / `async_pool()`. Stays at `scripts/` root, not this directory, pending the Postgres-dial locality work.
- `no_emoji.py`, `no_os_environ.py`, `no_script_sibling_imports.py` — Python conventions; sibling imports work with PYTHONSAFEPATH=1.
- Ruff S110 checks lone-pass handlers in production code, including typed exceptions.
- `ava_root_scope.py` — the `services/supervision/ava_root` boot program stays a separate codebase (ruling 2026-09-12): no permission-domain symbol may leak into its scope; `# ava-root-scope-ok: <reason>` opts a line out.
- `termination_source.py` — terminated writes stamp their source. Checks SQL/status literals and literal source binds against `TerminationSource`, rejecting `None`; runtime expressions need boundary validation.
- `clock_lattice.py` — lattice-vocabulary timing constants (STALL / GRACE / REAP / BUDGET / WEDGED / NO_PROGRESS / LAUNCH_CONFIRM / LEASE_TTL / LEASE_RENEW / SCAN_INTERVAL) may only be defined in the clock-lattice family modules, as aliases of a registered clock, or with an explicit exemption; the lattice topology itself lives in `base/deploy/timing.py`.
- `time_bomb.py` — tests may not exactly assert a value derived from a repo fixed-instant constant when the derivation can reach the real clock; pin the clock, use a tolerance, or opt out with `# time-bomb-ok:`. Source half threads a clock parameter into fixed-instant window boundaries; fixture half catches a fixed calendar literal bound to a window-shaped name.
- `agent_docstrings.py`, `agents_md_size.py` — agent-visible docstring / `AGENTS.md` size guards (the `_KNOWN_DIRTY_FILES` allowlist lives in the first script's header and the "SDK docstring discipline" section of `AGENTS.md`).
- `note_tags.py` — bidirectional NoteTag / timeline-marker contract: every backend tag has a frontend dispatch branch and every lifecycle, memory, or note dispatch member is a live backend tag.
- `plugins/no_plugin_wrap.py`, `plugins/no_ava_in_hooks.py` — plugin conventions: no bare monkey-patch of `ava.*` (declare an `SdkWrap`); no `import ava`, even deferred, in a hook module (`agent/hooks/`, an `agent_runtime.py` face, a module defining a `Hook` subclass), which operates on the graph `state` it is handed. No exemption in the second.
- `turn_scoped_config.py` — per-agent settings come from the agent's slices (`base/host/env/agent_slices.py`), not `settings`, `get_field` or a bare `resolve_setting`.
- `fixture_scope.py` — a pytest fixture may not mutate a process global at a scope that outlives its blast radius: (1) `scope="session"` outside `tests/fixtures/provisioning.py` plus any write to `os.environ` / a `settings` field / a module global; (2) `scope="package"` in a directory with no `__init__.py`, where pytest silently falls back to session scope. `tests/fixtures/provisioning.py` is the only exemption.
- `python_lock.py` — wraps `base/deploy/release/python_lock.py`: `uv.lock` needs PyPI registry and `files.pythonhosted.org` URLs; mirrors stay local. Pre-commit + `repo-language` CI; no project deps.
- `zombie_pyright_ignores.py` — a `# pyright: ignore[...]` whose named rule pyright no longer reports at that line is dead weight; `--check`/fix modes.

## `async_no_sync_blocking.py`

The gateway is a single event loop; one sync psycopg / subprocess / psutil
call inside an `async def` handler freezes every other request (the
2026-08-03 incident: `/api/memory/search` ran a synchronous embedding on the
loop and the gateway hung for hours).

Flags, inside an `async def` body (not a nested `def` — presumed threaded):
DB connection/cursor/execute/fetch/commit/rollback calls, known sync DB
helpers and sync ops, process/session backend calls, filesystem calls,
`shutil.`/`subprocess.`/`psutil.` module calls, `os.system`, `time.sleep`,
and any bare `*_blocking`-suffixed helper (those exist to be wrapped in
`asyncio.to_thread`). Inline opt-out: `# async-blocking-ok: <reason>`.

Scope: `gateway/` and `ops/` (the event-loop surfaces); pre-commit hook `lint-async-no-sync-blocking`.

## `logger_add_diagnose.py`

Every `logger.add(...)` sink outside test code must pass a literal
`diagnose=False`. loguru's `diagnose` defaults to True, so an unmarked sink
renders every local variable of a `logger.exception(...)` failure's frames —
DSNs, tokens, passwords — into that sink's output. Missing the kwarg, a
non-`False` literal, or a value this script cannot verify statically (a
name, a `**kwargs` unpack) are all flagged, fail-closed; no inline exemption
exists. Exempt: test code (`tests/`, `test_*.py`, `*_test.py`).

Wired into pre-commit (`lint-logger-add-diagnose`); rationale in the script docstring.

## CLI contract (explicit targets)

The `lint_*.py` gates that take path arguments share one contract: a typo'd target never scans silently, and an out-of-repo target scans rather than crashes.

- **No arguments** — scan the default scope (`_SCAN_DIRS`, the git-tracked file list, ...).
- **Explicit arguments must resolve.** A missing one is `error: target path(s) not found: <argument(s)>` on stderr, exit 1. Resolution is per-script: absolute paths as-is; a relative path against the repo root with a caller-cwd fallback (`lint_no_cjk` / `lint_no_tailnet`), against the repo root only (`time_bomb`), or against the caller's cwd (the rest; pre-commit passes absolute paths).
- **Out-of-repo targets scan under their absolute path.** Scope-anchored scripts (`code_structure` / `fixture_scope` / `plugins/no_plugin_wrap`) skip one outside their scope silently (rc 0). Directory targets enumerate members (`turn_scoped_config` takes `.py` files only, so a directory scans nothing); an unreadable member (dangling symlink, non-UTF-8) is skipped. `time_bomb` resolves callees through its repo-scoped index, so an out-of-repo source file's source half silently passes; only its test half applies.

## `no_silent_failures.py`

[[scripts/lint/docs/no-silent-failures.ava.okf.md]].

## `loguru_format.py`

Message-format and lost-traceback rules for loguru vs stdlib loggers — see
[[scripts/lint/docs/loguru-format.ava.okf.md]].

Parent: [[scripts/docs/scripts.ava.okf.md|scripts]].
