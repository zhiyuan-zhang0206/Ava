# Test patch audit

Data for the dependency-injection work that follows the move of tests into their
packages: which private names of other packages tests replace, from which packages, and how
often. It is a census, not a design: what a seam looks like for a given target is decided
per package when that package's tests are worked through.

The numbers are the output of `scripts/lint/patch_targets.py --report` on `ab1ca1c0a`
(839 test files with patch points). Regenerate after every package move or injection
refactor: the class D counts fall as seams appear, and the frozen `patch_targets` section of
`scripts/structure/baseline/` (325 keys, 685 sites now) shrinks in step. Rule and
fixes: [python-conventions](../../docs/conventions/python-conventions.md), Rule 8.

## The five classes

Every patch point of a test file (`monkeypatch.setattr` / `delattr` / `setitem` with a string
or an object target, `patch`, `patch.object`, `patch.dict`, `patch.multiple`, `mocker.patch`,
also as decorators and `with` blocks) lands in exactly one class. A test's *home* is the
deepest package that holds or directly depends on every module the test references
(`scripts/structure/placement.py`), not the directory it sits in: a test of `a.x` and `b.y`
lives in `b` when `b`'s production code imports `a`. The home follows production imports, so
the census moves when they do.

- **A, environment boundary.** Not repository code: stdlib, third-party, the runtime, the
  test harness, or an environment variable that is not an `AVA_*` setting.
- **B, own package.** Repository code whose owning package contains the test's home. The
  owner of a private name is the package that holds its first private component (Rule 4); the
  owner of a public name is the package of its module.
- **C, another package's public name.** Deep attributes (`module.Class.method`) are counted
  separately and are not violations yet.
- **D, violation.** A private name (a single leading underscore, on an attribute or on a
  module segment) whose owning package does not contain the test's home. Reported by relation:
  `ancestor` (the test lives in an ancestor package and reaches into a descendant's private),
  `other-unit`, `sibling` (same unit, the home is in another sub-package than the owner),
  `top-level` (a test with no package home).
- **E, global environment.** The explicit list `E_MODULES` in
  `scripts/structure/patch_targets.py` (settings, paths, machine and cluster identity, env
  resolution, ambient services) and `AVA_*` environment variables. Tests replace these through
  the same few seams everywhere; a test-support design for them is a separate line of work,
  planned in [dependency injection](dependency-injection.md).
- **U, unresolved.** The patched object is not statically known (a parameter, a call result).

## Totals

| class | meaning | points |
|---|---|---|
| A | environment boundary (stdlib, third-party, runtime, non-AVA env vars) | 1763 |
| B | own package (the owning package contains the test's home) | 3204 |
| C | another package's public name | 2030 |
| D | violation: a private name of a package the test does not belong to | 685 |
| E | global environment (settings, paths, identity, env resolution, ambient services) | 3320 |
| U | unresolved (object not statically known) | 239 |
|  | total patch points | 11241 |

Class C includes 88 deep attributes (`module.Class.method`), not violations.

## Patch points by test home (top 15)

| test home | files | points | A | B | C | D | E | U |
|---|---|---|---|---|---|---|---|---|
| `cli/commands/lifecycle` | 25 | 601 | 50 | 225 | 172 | 44 | 108 | 2 |
| `gateway` | 56 | 592 | 38 | 25 | 224 | 54 | 250 | 1 |
| `services/agent_runner/agent_host` | 45 | 509 | 28 | 192 | 134 | 54 | 99 | 2 |
| `ava` | 24 | 468 | 58 | 79 | 49 | 71 | 201 | 10 |
| `base/lm` | 11 | 374 | 145 | 45 | 1 | 2 | 181 | 0 |
| `cli/commands/data_plane` | 13 | 357 | 33 | 162 | 42 | 23 | 97 | 0 |
| `cli` | 27 | 338 | 53 | 42 | 49 | 55 | 136 | 3 |
| `base` | 25 | 332 | 55 | 0 | 62 | 55 | 149 | 11 |
| `cli/commands/cluster` | 8 | 281 | 24 | 137 | 41 | 3 | 76 | 0 |
| `ops` | 12 | 270 | 19 | 60 | 123 | 29 | 38 | 1 |
| `agent/graph` | 19 | 257 | 6 | 35 | 52 | 4 | 158 | 2 |
| `gateway/routers` | 13 | 247 | 30 | 64 | 92 | 3 | 58 | 0 |
| `base/telemetry` | 6 | 244 | 44 | 23 | 19 | 10 | 148 | 0 |
| `cli/parsers` | 11 | 239 | 28 | 0 | 149 | 20 | 42 | 0 |
| `cli/commands` | 14 | 222 | 22 | 2 | 49 | 58 | 85 | 6 |

## Class D

685 points, 325 distinct (file, target) keys, 151 files.

| relation | meaning | points |
|---|---|---|
| ancestor | the test's home is a strict ancestor of the owner (patches a descendant's private) | 344 |
| other-unit | the owner is in a different top-level unit | 268 |
| sibling | same unit, another lineage | 63 |
| top-level | the test has no package home | 10 |

### By test home (top 15)

| test home | D points |
|---|---|
| `ava` | 71 |
| `cli/commands` | 58 |
| `base` | 55 |
| `cli` | 55 |
| `services/agent_runner/agent_host` | 54 |
| `gateway` | 54 |
| `cli/commands/lifecycle` | 44 |
| `ops` | 29 |
| `ava_builtins/plugins` | 25 |
| `cli/commands/data_plane` | 23 |
| `cli/parsers` | 20 |
| `agent` | 18 |
| `scripts` | 18 |
| `agent/graph/claim` | 16 |
| `cli/commands/converge` | 15 |

### By production module (top 20)

Production modules whose private names tests reach into, with the private names most often
patched and the test homes that patch them. This is the injection-seam work list.

| production module | points | private names | patched from (test home) |
|---|---|---|---|
| `ava.mcps` | 43 | `_read_cache` 10, `_get_remote_client` 9, `_sessions` 8 | `ava` 40, `agent` 3 |
| `agent.impersonation` | 32 | `_PROCESS_STARTED_MONOTONIC` 9, `_spawn_codex_relay` 8, `_provider_anchor_states` 6 | `services/agent_runner/agent_host` 32 |
| `cli.commands.extensions.external_skills` | 28 | `_rename_no_replace` 20, `_stage_copy` 3, `_commit_activation` 2 | `cli/commands` 28 |
| `base.deploy.git.memory_repo` | 21 | `_run_git` 13, `_download_pool_snapshot` 4, `_init_local_repo` 2 | `base` 21 |
| `ops.lifecycle` | 21 | `_recovery_halt_reason` 5, `_wake_suppression_active` 3, `_recovery_halted` 3 | `ops` 21 |
| `services.desktop.permissions_helper.lifecycle` | 19 | `_BUILD_DIR` 3, `_keychain_lock_reason` 3, `_interactive_signing_reason` 3 | `cli/commands/lifecycle` 19 |
| `ava.mcps._remote` | 18 | `_remote` 18 | `ava` 18 |
| `scripts.lint.async_no_sync_blocking` | 18 | `_REPO_ROOT` 9, `_ROOT` 2, `_DEFINITION_DIRS` 2 | `scripts` 18 |
| `agent.startup` | 16 | `_page_server_alive` 9, `_last_reconcile_at` 7 | `agent/graph/claim` 16 |
| `cli.commands.lifecycle.root_driver` | 16 | `stop_root_service_tree` 8, `root_tree_selection` 4, `_root_tree_plan` 2 | `cli/commands/data_plane` 10, `cli` 4, `cli/commands/observability` 1, `(top-level)` 1 |
| `cli.commands.lifecycle._temporary_stop` | 15 | `_temporary_stop` 15 | `cli` 15 |
| `base.daemon.health` | 14 | `_probe_daemon` 5, `_probe_home` 3, `_health_payload` 3 | `base` 9, `cli/commands` 2, `services/supervision/healthchecks` 2, `services/upkeep/events_maintenance` 1 |
| `cli.commands.observability.lgtm_native` | 14 | `_download_and_verify` 4, `_verify_loki` 3, `_stream_download` 2 | `cli/commands` 9, `cli/commands/converge` 5 |
| `ops.agent_pause` | 14 | `_wake` 7, `_lifecycle_wait_seconds` 4, `_LIFECYCLE_WAIT_POLL_SECONDS` 3 | `agent/ownership` 8, `services/agent_runner/agent_host` 3, `cli/commands/lifecycle` 1, `cli/parsers` 1 |
| `base.telemetry.otlp.telemetry_otlp` | 11 | `_OtlpBackend` 8, `_build_providers` 3 | `base/telemetry` 8, `agent` 2, `base` 1 |
| `gateway.alerts.router` | 10 | `notify_im` 10 | `cli/commands` 10 |
| `gateway.cluster.status` | 10 | `_compute_stats_dashboard` 8, `_probe_agent_runner` 1, `_STATUS_CACHE_TTL_S` 1 | `gateway` 10 |
| `services.desktop.permissions_helper.launchd_job` | 10 | `_retirement_command` 4, `_executable_pids` 2, `_retirement_owner` 2 | `cli/commands/lifecycle` 10 |
| `ava.shell.sessions` | 9 | `_next_session_index_from_db` 3, `_shell_prefix` 3, `_record_ttl` 3 | `ava` 6, `cli/commands/data_plane` 3 |
| `cli.commands.agents.impersonation_relay` | 9 | `_MIN_EMIT_INTERVAL_SECONDS` 2, `_read_inbox` 2, `_write_heartbeat` 2 | `cli/parsers` 9 |

## Class E by module (top 15)

| ambient module | tier | points | test homes |
|---|---|---|---|
| `base.config` | config | 1676 | 90 |
| `env:AVA_*` | env-var | 411 | 40 |
| `base.paths` | paths | 375 | 55 |
| `base.cluster.machine` | identity | 236 | 29 |
| `ava.sdk_surface.agent_identity` | identity | 99 | 14 |
| `base.log` | ambient-service | 87 | 18 |
| `base.telemetry` | ambient-service | 84 | 31 |
| `base.db` | ambient-service | 81 | 31 |
| `base.host.env.runtime_config` | env-resolution | 80 | 17 |
| `base.host.env.dotenv_boot` | env-resolution | 73 | 7 |
| `base.cluster` | identity | 68 | 17 |
| `base.host.env.bootstrap` | env-resolution | 50 | 9 |

## Files placed by the patch-evidence fallback

Every strong first-party reference of these files is patch evidence, so they keep it (see `scripts/structure/placement.py`): `base/config/tests/test_seed_guard.py`, `tests/ava/test_self_evolution_schedules.py`, `tests/schedules/test_c9_daily_report.py`, `ava/tests/test_self_evolution_evaluate.py`.
