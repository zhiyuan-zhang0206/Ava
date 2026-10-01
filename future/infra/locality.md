# Locality — from convention to structure

Goal: **local reasoning** — a correct change needs only the package being changed
plus the public doors of its neighbors. The mechanism that enforces the first
two legs (package doors, single decision owners) is the structure gate's Rules 4
and 5 (`scripts/lint/code_structure.py`, `scripts/structure/locality.py`); how to
work with them is in [python-conventions](../../conventions/python-conventions.md).
Rule 8 (`scripts/lint/patch_targets.py`) applies the same idea to tests: a test may
not patch another package's private names; its census, the injection-seam work
list, is [test-patch-audit](test-patch-audit.md).
The principle itself lives in the serious-engineering skill
(`principles/complexity-management`, "Locality is information hiding made
observable"). The sweeper's `locality` class (`scripts/structure/cochange.py`)
indexes per-change package spread and cross-package co-change pairs. This page
tracks what is **left**.

## Calibration snapshot (2026-09-26)

Squash commits on `main` since the 2026-08-19 public cutover (2284), source files
only (tests, docs, generated files excluded), module = path depth 2:

| type | commits | src files p50 / p90 | modules p50 / p90 | >= 5 src files |
|---|---|---|---|---|
| fix | 1073 | 2 / 6 | 1 / 4 | 16% |
| feat | 437 | 5 / 14 | 3 / 7 | 52% |
| refactor | 61 | 8 / 30 | 4 / 11 | 74% |

The fix share touching >= 5 source files trended up week over week (13% -> 19%).
The size budgets are not the cause: only 24 file pairs (with >= 8 shared
commits) co-change >= 60% of the time. The leaks are inter-module: decisions
with no single owner (the Postgres dial, the process identity key), hub
registries (`base/config`,
`cli/main.py` <-> `cli.parsers`), and same-process splits by technical layer
(`cli/commands` <-> `cli/parsers`, `gateway/routers` <-> `gateway/schemas`).
Cross-process pairs (`gateway/schemas` <-> `ui/web`) are real contract
boundaries, already carried by codegen, and not debt.

## Left, in order

Most of what remains sits in files the long-running unified-cluster-lifecycle
branch (#3479) rewrites — its data-plane credentials, updater and process
lifecycle; those items wait for it to land and are then designed on its code.

1. **Postgres door burn-down** (`owner_bypasses`, 19 modules / 29 sites).
   Extend `base.db.connect()` / `pool()` so the door owns the transport
   posture while the caller owns the target (an explicit URL for provisioning,
   PITR, and restore drills), and add the async pool factory the agent host
   and eval pools lack. Migrate the sites; genuine exceptions become reasoned
   `allowed` entries. Collapse the three drifted `watch_idle.py` reference
   copies into one. Then narrow `scripts/lint_pool_keepalives.py` to what
   Rule 5 does not cover, or retire it. Waits for #3479, which rewrites
   `base/db_connections.py` (owner-admin, executor-admin and
   generation-login dials) and most of the dialing modules — design the named
   entry points on its version.
2. **Reach-in burn-down** (`private_imports`, 23 keys / 25 sites / 20 files).
   Every remaining key sits in a file #3479 rewrites. Highest yield once it
   lands: `base.agents.impersonation._store` (5 sites). `ava` carries no frozen reach-ins: agent
   visibility there is the `__all_for_ava__` whitelist (which
   `agent_docstrings` keys on too), not the underscore, so every
   framework module or name another package needs took a public name without
   entering the agent's view.
3. **Directory budgets.** `cli/commands` is down to 86 entries: an empty
   package door plus domain subpackages (`agents`, `management`, `extensions`,
   `observability`, `data_plane`, `cluster`, `converge`), with converge steps
   living beside the domain they converge. The remaining `lifecycle/` and
   `update/` split waits for #3479, which deletes and renames most of those
   modules; so does `base/` (231), the largest over-budget directory.
   `agent/graph` is down to 35: one package per node, with `claim/` and `llm/`
   done; the exec family (`_exec*`, 10 modules) is next and waits for #3479,
   which rewrites four of them. `base/lm` is back under budget. The
   `python -m cli.commands._*` process entry points are a cross-version
   contract (the ops server composes the command a possibly different checkout
   runs) and do not move without an expand-contract step.
4. **More single-owner decisions**, each added to `DECISIONS` only once its
   owner exists: the OS process identity key (pid + kernel start time; one fix
   touched 28 files across exec ownership, the updater, PITR and PTY sessions;
   natural owner `base/native_process/ownership.py`'s `OwnedProcess`) is the next
   candidate — most of its readers sit in files #3479 rewrites.
   `base/config` as a registration hub needs a design pass first.
5. **Tests in the top-level `tests/`** (`scripts/structure/tests_location.py`, the
   `tests_location` section: 209 frozen tests; `tests_location_allowed.py`: 63 registered, 58
   `contract` and 5 `integration`). The lint refuses a new top-level test that is not e2e, UI or
   registered, by path alone; the frozen tests are the work list for moving tests into their
   packages (`--suggest <file>` names each one's lowest legal package, the move tool's job).
   Still open: (a) **legality of tests inside packages** (a test whose package may not import what it
   uses, or that holds none of the code it tests): it needs `place()` and the import-linter
   contracts, so it is a separate check, not a hook; today three package tests would fail it
   (`scripts/tests/test_tools_scratch_home.py`, and two under
   `services/gateway_side/backup/tests/` that use both the loose `services/backup.py` and
   `services.gateway_side`); (b) `tests/fixtures/path_scopes.py` still gives autouse isolation
   fixtures by directory prefix, so a test moved into a directory the table does not list loses
   them (the lint's message says so; `tests/ci/test_path_scopes.py` catches a listed test that
   lost them, not a new test placed in an unlisted directory); (c) whether a frozen or registered
   entry still needs its place (a frozen test that has since lost its package home) needs the
   placement rule, so it belongs in a slow CI check, not a hook.
