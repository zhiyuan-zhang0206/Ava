# Locality — from convention to structure

Goal: **local reasoning** — a correct change needs only the package being changed
plus the public doors of its neighbors. The mechanism that enforces the first
two legs (package doors, single decision owners) is the structure gate's Rules 4
and 5 (`scripts/lint_code_structure.py`, `scripts/structure/locality.py`); how to
work with them is in [python-conventions](../../conventions/python-conventions.md).
The principle itself lives in the serious-engineering skill
(`principles/complexity-management`, "Locality is information hiding made
observable"). This page tracks what is **left**.

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
registries (`shared/config`,
`cli/main.py` <-> `cli.parsers`), and same-process splits by technical layer
(`cli/commands` <-> `cli/parsers`, `gateway/routers` <-> `gateway/schemas`).
Cross-process pairs (`gateway/schemas` <-> `ui/web`) are real contract
boundaries, already carried by codegen, and not debt.

## Left, in order

1. **Postgres door burn-down** (`owner_bypasses`, 29 modules / 61 sites). Extend
   `shared.db.connect()` / `pool()` so the door owns the transport posture while
   the caller owns the target (an explicit URL for provisioning, PITR, and
   restore drills), and add the async pool factory the agent host and eval
   pools lack. Migrate the sites; genuine exceptions become reasoned `allowed`
   entries. Collapse the three drifted `watch_idle.py` reference copies into
   one. Then narrow `scripts/lint_pool_keepalives.py` to what Rule 5 does not
   cover (`scripts/`, and any async pool left in `allowed`), or retire it if
   nothing remains.
2. **Reach-in burn-down** (`private_imports`, 298 keys / 315 sites / 109
   files), highest yield first: `cli/main.py` re-exports 121 private
   `cli.parsers.*._h_*` handlers so tests have one namespace to patch (a
   hand-maintained registry — bind handlers in their parser modules and patch
   there); then the most reached-into privates (`shared.lm._effort`,
   `shared.lm._plugin_providers`, `shared.agents.impersonation._impersonation_store`,
   `agent.graph._exec_protocol`, `agent._turn_progress`) each get a verdict:
   contract (export it) or internal (route callers through a door).
   In `ava` (12 keys / 18 sites left), agent visibility is the
   `__all_for_ava__` whitelist — `lint_agent_docstrings` keys on it too — so a
   framework module the rest of the repo needs takes a public name without
   entering the agent's view (`ava/agent_identity.py` was the first, then
   `sdk_validation`, `composer_commands`, `attachment_transport`,
   `skill_sources`, `sdk_metering`, `external_state`, `gateway_client` —
   whose `post` / `patch` / `raise_from_response` plugins also use —
   `impersonation_replay`, `impersonation_launch`, and `mcp_config` — whose
   `read_servers` / `machine_config_path` / `builtin_mcp_paths` `cli` / `ops`
   also reach through). The same underscore-drop applies to a private NAME
   (not a whole module) reached across a package boundary inside `ava`:
   `ava.shell.sessions.{current_session_generation, session_generation, reap,
   create_session}`, the `ava.shell.background` submodule, `ava.files.resolve`,
   `ava.agents.spawn_impl`, and `ava.skills.names` — none entered
   `__all_for_ava__` either. Left, in
   order: `_extend`, whose public name
   is taken by the plugin-author `ava.extend` namespace; and the kernel's reads
   and writes of `ava/__init__.py` private render state (`_COMPACT_CLASSES`,
   `_hidden_surface_members`, the SDK-disable entries).
3. **Locality sweeper class** — an index, not a wall: per-PR module spread and
   cross-package co-change pairs over a rolling window, reusing the lint's
   scanner per [lint-vs-sweeper](../../conventions/lint-vs-sweeper.md). Each
   finding names the leaked decision and lands as a ledger entry.
4. **Contract snapshot.** A public-API snapshot for the core doors
   (`shared.db`, `shared.agents`, `shared.events`) next to the existing
   `ui/web/openapi.json` and `db/schema.sql` snapshots. A snapshot diff needs an
   explicit declaration in the PR; a `fix` that must change a contract is a
   design bug and goes back to align rather than landing as an internal fix.
5. **PR description locality note.** When a PR's source changes span three or
   more modules, the description names the leaked decision and either closes
   it or files a task (`write-a-pr-description` skill).
6. **More single-owner decisions**, each added to `DECISIONS` only once its
   owner exists: the process identity key (one fix touched 21 files; its
   natural owner is `ava/agent_identity.py`, now a public module) is the
   next candidate; `shared/config` as a registration hub needs a design pass
   first.
