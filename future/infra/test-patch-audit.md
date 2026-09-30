# Test patch audit

Data for the dependency-injection work that follows the move of tests into their
packages: which private names of other packages tests replace, from which packages, and how
often. It is a census, not a design: what a seam looks like for a given target is decided
per package when that package's tests are worked through.

The numbers are the output of `scripts/lint/patch_targets.py --report` on `6e6347dbb`
(904 test files with patch points). Regenerate after every package move or injection
refactor: the class D counts fall as seams appear, and the frozen `patch_targets` section of
`scripts/structure/baseline/` (642 keys, 1472 sites now) shrinks in step. Rule and
fixes: [python-conventions](../../conventions/python-conventions.md), Rule 8.

## The five classes

Every patch point of a test file (`monkeypatch.setattr` / `delattr` / `setitem` with a string
or an object target, `patch`, `patch.object`, `patch.dict`, `patch.multiple`, `mocker.patch`,
also as decorators and `with` blocks) lands in exactly one class. A test's *home* is the
package its own first-party imports place it in, not the directory it sits in.

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
  `other-unit`, `sibling`, `top-level` (a test with no package home).
- **E, global environment.** The explicit list `E_MODULES` in
  `scripts/structure/patch_targets.py` (settings, paths, machine and cluster identity, env
  resolution, ambient services) and `AVA_*` environment variables. Tests replace these through
  the same few seams everywhere; a test-support design for them is a separate line of work.
- **U, unresolved.** The patched object is not statically known (a parameter, a call result).

## Totals

| class | meaning | points |
|---|---|---|
| A | environment boundary (stdlib, third-party, runtime, non-AVA env vars) | 1892 |
| B | own package (the owning package contains the test's home) | 2633 |
| C | another package's public name | 2443 |
| D | violation: a private name of a package the test does not belong to | 1472 |
| E | global environment (settings, paths, identity, env resolution, ambient services) | 3414 |
| U | unresolved (object not statically known) | 282 |
|  | total patch points | 12136 |

Class C includes 150 deep attributes (`module.Class.method`), not violations.

## Patch points by test home (top 15)

| test home | files | points | A | B | C | D | E | U |
|---|---|---|---|---|---|---|---|---|
| `base` | 90 | 1318 | 333 | 0 | 237 | 144 | 586 | 18 |
| `cli` | 73 | 997 | 115 | 42 | 361 | 221 | 244 | 14 |
| `gateway` | 76 | 944 | 75 | 28 | 300 | 206 | 333 | 2 |
| `cli/commands` | 31 | 887 | 75 | 78 | 222 | 245 | 261 | 6 |
| `ava` | 30 | 618 | 91 | 114 | 65 | 121 | 217 | 10 |
| `services/agent_host` | 45 | 511 | 28 | 192 | 136 | 54 | 99 | 2 |
| `cli/commands/data_plane` | 16 | 433 | 34 | 218 | 54 | 12 | 115 | 0 |
| `ops` | 20 | 375 | 24 | 50 | 176 | 67 | 57 | 1 |
| `agent` | 34 | 304 | 20 | 16 | 97 | 74 | 95 | 2 |
| `services/agent_ops` | 9 | 213 | 4 | 115 | 31 | 3 | 59 | 1 |
| `cli/commands/converge` | 12 | 210 | 33 | 51 | 75 | 10 | 38 | 3 |
| `scripts` | 16 | 202 | 95 | 26 | 11 | 32 | 3 | 35 |
| `services/pitr` | 16 | 190 | 34 | 2 | 50 | 39 | 59 | 6 |
| `agent/graph` | 12 | 189 | 8 | 13 | 29 | 10 | 129 | 0 |
| `cli/commands/lifecycle` | 12 | 187 | 17 | 29 | 72 | 41 | 26 | 2 |

## Class D

1472 points, 642 distinct (file, target) keys, 267 files.

| relation | meaning | points |
|---|---|---|
| ancestor | the test's home is a strict ancestor of the owner (patches a descendant's private) | 1067 |
| other-unit | the owner is in a different top-level unit | 384 |
| sibling | same unit, another lineage | 11 |
| top-level | the test has no package home | 10 |

### By test home (top 15)

| test home | D points |
|---|---|
| `cli/commands` | 245 |
| `cli` | 221 |
| `gateway` | 206 |
| `base` | 144 |
| `ava` | 121 |
| `agent` | 74 |
| `ops` | 67 |
| `services/agent_host` | 54 |
| `cli/commands/lifecycle` | 41 |
| `ava_builtins/plugins` | 40 |
| `services/pitr` | 39 |
| `scripts` | 32 |
| `ava/gateway_client` | 13 |
| `cli/commands/data_plane` | 12 |
| `ava_builtins/plugins/ava_memory` | 11 |

### By production module (top 20)

Production modules whose private names tests reach into, with the private names most often
patched and the test homes that patch them. This is the injection-seam work list.

| production module | points | private names | patched from (test home) |
|---|---|---|---|
| `cli.commands.cluster.health` | 71 | `_gateway_liveness_with_retry` 14, `_service_probes` 9, `_agent_population` 8 | `cli/commands` 63, `cli` 8 |
| `cli.commands.lifecycle.root_driver` | 69 | `_stop_root_service_tree` 15, `_root_client` 10, `_launch_service_tree` 9 | `cli/commands` 56, `cli` 11, `cli/commands/observability` 1, `(top-level)` 1 |
| `cli.release_transition.launcher_linux` | 66 | `_command` 29, `_properties` 19, `_boot_id` 5 | `cli` 66 |
| `base.host.net.resilience` | 49 | `_sleep` 29, `_asleep` 13, `_agent_phase` 7 | `ava/gateway_client` 13, `services/memory_indexer/embeddings` 10, `base/lm` 7, `services/browser` 5 |
| `ava.mcps` | 43 | `_read_cache` 10, `_get_remote_client` 9, `_sessions` 8 | `ava` 40, `agent` 3 |
| `ops.lifecycle` | 40 | `_cluster_rpc` 16, `_recovery_halt_reason` 5, `_cancel_hosted_turn_best_effort` 4 | `ops` 35, `agent` 3, `gateway/agents` 1, `services/delivery_watchdog` 1 |
| `ava.mcps._daemon` | 39 | `_daemon` 39 | `ava` 39 |
| `ava.agents` | 38 | `_client` 38 | `gateway` 21, `ava_builtins/plugins` 15, `agent` 2 |
| `agent.impersonation` | 32 | `_PROCESS_STARTED_MONOTONIC` 9, `_spawn_codex_relay` 8, `_provider_anchor_states` 6 | `services/agent_host` 32 |
| `cli.commands.extensions.external_skills` | 28 | `_rename_no_replace` 20, `_stage_copy` 3, `_commit_activation` 2 | `cli/commands` 28 |
| `gateway.cluster.status` | 25 | `_cluster_rpc` 15, `_compute_stats_dashboard` 8, `_probe_agent_runner` 1 | `gateway` 25 |
| `agent.graph.exec.node` | 21 | `_run_in_subprocess` 12, `_run_agent_code` 9 | `agent` 14, `ava_builtins/plugins/ava_syntax_fix` 7 |
| `base.deploy.git.memory_repo` | 21 | `_run_git` 13, `_download_pool_snapshot` 4, `_init_local_repo` 2 | `base` 21 |
| `cli.commands.lifecycle._temporary_stop` | 21 | `_temporary_stop` 21 | `cli` 21 |
| `cli.commands.lifecycle.stop` | 21 | `_do_stop` 9, `_reap_cluster_chrome` 3, `_release_self_heal_pause` 3 | `cli` 18, `cli/commands` 3 |
| `gateway.agents.router` | 21 | `_forward_spawn_to_remote` 20, `_mark_launch_failure` 1 | `gateway` 21 |
| `services.permissions_helper.lifecycle` | 21 | `_expected_dr` 4, `_BUILD_DIR` 3, `_keychain_lock_reason` 3 | `cli/commands/lifecycle` 19, `cli` 2 |
| `services.pitr.restore.drill` | 20 | `_require_group_leader` 2, `_live_identity` 2, `_prepare_pgdata` 2 | `services/pitr` 20 |
| `cli.commands.observability.lgtm_native` | 19 | `_load_versions` 5, `_verify_loki` 4, `_download_and_verify` 4 | `cli/commands` 19 |
| `gateway.routers.fleet_graph` | 19 | `_monotonic` 6, `_fetch_loki_edges` 4, `_fetch_archive_edges` 3 | `gateway` 19 |

## Class E by module (top 15)

| ambient module | tier | points | test homes |
|---|---|---|---|
| `base.config` | config | 1707 | 76 |
| `env:AVA_*` | env-var | 414 | 34 |
| `base.paths` | paths | 410 | 51 |
| `base.cluster.machine` | identity | 250 | 24 |
| `ava.agent_identity` | identity | 99 | 13 |
| `base.log` | ambient-service | 87 | 17 |
| `base.telemetry` | ambient-service | 84 | 26 |
| `base.db` | ambient-service | 83 | 23 |
| `base.host.env.runtime_config` | env-resolution | 83 | 15 |
| `base.host.env.dotenv_boot` | env-resolution | 74 | 6 |
| `base.cluster` | identity | 73 | 15 |
| `base.host.env.bootstrap` | env-resolution | 50 | 9 |

## Files placed by the patch-evidence fallback

Every strong first-party reference of these files is patch evidence, so they keep it (see `scripts/structure/placement.py`): `tests/_pitr_fixtures.py`, `tests/ava/test_seed_guard.py`, `tests/ava/test_self_evolution_schedules.py`, `tests/schedules/test_c9_daily_report.py`, `tests/skills/test_self_evolution_evaluate.py`.
