# One cluster per host: the deletion plan

**Status: slices 1 to 5 are done; slice 6 is partly done.** The ruling and its
reasons are in
[one cluster per host](../../decisions/2026-09-30-one-cluster-per-host.md). This
doc is the order of work: six slices, each its own PR with its docs and tests.
The shipped model is described in [`AGENTS.md`](../../AGENTS.md) and
[`runbook.md`](../../conventions/runbook.md); this doc stays as the record of the
order and of what each slice deleted.

Slice 6 has its Linux container recipe (`scripts/verify/`) and the Tart
clone-inheritance experiment. Still to do: the Tart recipe is a design with no
script (the golden image build and the per-run clone driver do not exist), and
`ava start` end to end inside a macOS guest has not been run; the open items are
in [verification boundaries](verification-boundaries.md#not-yet-known).

## Boundary

A host runs one cluster at `~/.ava`. Anything that verifies something runs in a
container, a VM or a Tart macOS VM, where it is that boundary's one cluster.
In scope: the mechanisms that keep several clusters apart on one host, and the
way a process learns its home. Out of scope: the release path and PITR shell
(removed by the updater work,
[decision](../../decisions/2026-09-30-remove-release-image-path.md)), the docs
and tests relocation (the locality work), and the data plane itself.

## What must survive every slice

1. **The production home keeps resolving.** The production source checkout owns
   `~/.ava`. Existing production `start-intent.json` files carry a `worktree`
   field and a `record` with ports; a slice either reads them or names, in its
   PR, the hand step that rewrites them before the first start. No
   compatibility code is added for them.
2. **Development code never drives the host's cluster.** A test session sets
   `AVA_HOME` before it spawns anything; hooks and tools that import application
   code set a scratch `AVA_HOME`; the `~/.ava` cluster is started and stopped
   only from `~/.ava/source`. Each rule has a test that fails if it is broken.
3. **`AVA_HOME` is the one override of the home.** Unset means `~/.ava`. It is
   read when needed, never captured at import, and there is no second channel.
4. **A test home never uses the production ports.** Tests on a machine that also
   runs production bind and dial only ports of their own.
5. **Merge is not deploy.** Every slice lands in `main`; the operator rolls it
   out, with a full stop and start per host.

## Slices

| # | Status | Delete | Main files | Risk |
|---|---|---|---|---|
| 1 | done (#3764) | The native local preview controller, its mock and validation tasks, its tests and the lint allowlist entry | `scripts/preview/**` (15 files), six of the seven tests in `tests/lifecycle/preview/` (`test_visual_gate.py` stays), `scripts/lint/no_os_environ.py`, docs and skills that name it | Leaf: one importer outside itself, no production path |
| 2 | done (#3776; #3785 extended the scratch-home check to every script) | `ava start --worktree`, the `.ava_home` pointer, checkout binding and lock, the env-versus-pointer contradiction check, `AVA_HOME_OVERRIDE`, the unanchored scratch home. The home becomes `AVA_HOME` or `~/.ava`, read when called; the import-time users of the home become lazy; only `~/.ava/source` may start or stop `~/.ava`; hooks and tools set a scratch home | `base/host/env/dotenv_boot.py`, `base/paths`, `cli/start_identity.py`, `cli/start_intent.py`, `cli/parsers/host.py`, `tests/fixtures/env_bootstrap.py`, the hook configuration | Start path, and every tool that relied on the scratch home. Existing intents carry `worktree` |
| 3 | done (#3774) | Port-block allocation and cross-cluster preflight; `--health-port-base`, `health_port_env` and the WSL default base; the retired `restarter` and `coordinator` slots. New homes record the fixed port table (25 slots, `base/host/env/port_table.py`); a home keeps reading its recorded ports; tests keep ports of their own (the session's kernel-assigned table for homes born in tests) | `base/cluster/{ports,port_preflight,derive}.py`, `base/host/env/{port_table,registry}.py`, `cli/commands/converge/{port_preflight,health_preflight}.py`, `cli/preflight.py`, `cli/start_identity.py`, `cli/start_intent.py`, `cli/parsers/host.py`, and the 10 consumers of `record_*_port`, now `rec.ports[...]` (`base/db/{connections,pg_admin}.py`, `cli/commands/data_plane/{bringup,cluster_instance,pgbouncer}.py`, `cli/commands/converge/redis_bridge.py`, `cli/commands/observability/otel_collector.py`, `services/ava_root_glue/diagnostic_probes.py`, `services/pitr/activation/runtime.py`, `scripts/data_plane_ops/rotate_data_plane_secrets.py`) | Bring-up reads recorded ports unchanged. A record that still carries the two retired slots is refused at start, so they are deleted from `start-intent.json` by hand between the stop and the start |
| 4 | done (#3787) | The bare-`ava` launcher routing, the per-home CLI link, `AVA_HOST_STATE_DIR` (host state is the home), per-home OS job slugs; `ava` on PATH becomes a direct link to the production source CLI. `cluster down` is deleted and `cluster destroy` loses `--path`: it acts on this home, the default one included, and needs an interactive confirmation (type the home path, no bypass flag). Fixed OS job labels need a gate in the same slice: only the default home registers or removes OS jobs. The permissions helper keeps its per-home launchd label for now (a fixed label changes its launchd identity on every Mac, to be taken on its own) | `scripts/ava-launcher.sh`, `cli/commands/converge/{_steps,host}.py`, `cli/commands/cluster/home.py`, `base/config/general.py`, `base/paths`, `base/host/system/*`, `tests/base/test_bare_ava.py` | Host wiring. Converge rewrites the PATH link and the crontab lines on the next start; the old launchd jobs and the old Linux boot unit are removed by hand between the stop and the start |
| 5 | done (#3795) | The one-step birth inside `ava start`: `ava init` takes the first-start flags (11 of `start`'s 15; `--worktree` and `--health-port-base`, the other two of the original 17, went with slices 2 and 3), writes the home's identity and starts nothing; `start` keeps service selection and fails fast on an uninitialized home. The data plane is still born by the first `start` (the birth branches follow the intent's phase). `--db-capability` also serves a runner's later bundle, so it moves to `ava cluster db-authority install-unit` beside `issue-unit` | `cli/parsers/host.py`, `cli/init_intent.py` (new), `cli/unit_join.py` (new), `cli/start_intent.py`, `cli/start_identity.py`, `cli/commands/lifecycle/start.py`, `cli/commands/_setup.py`, `cli/commands/cluster/control.py`, `scripts/verify/container.py` | Small: the identity half of `start` was already a settings-free phase. No intent change, so no hand step on a running home |
| 6 | partly done: container recipe (#3779), Tart experiment (#3782) | Nothing is deleted: the container and Tart recipes that replace the native preview (a Linux image running the normal stack, a Tart base image with its one-time grants) | new: [verification boundaries](verification-boundaries.md); the container recipe is `scripts/verify/`; the Tart clone-inheritance experiment passed (Experiment B there: a clone of a granted image keeps the helper identity and both grants); the Tart recipe is not scripted and `ava start` end to end in a guest has not been run | Separate design; slices 1 to 5 do not wait for it |

Dependency order: slice 1 first, a pure deletion that removes the largest
consumer of everything after it. Slice 3 is implemented against whatever of
slice 2 has merged. Slice 4 follows slice 2, and slice 5 follows 2 to 4. Slice 6
is independent of them.

## Coordination

Nothing is held back for other work. The release path the updater work was
deleting is gone
([decision](../../decisions/2026-09-30-remove-release-image-path.md)), and slice 4
was the last to touch `base/cluster` and `base/host/env`, so the tests that
exercise these mechanisms, the pinned `tests/base` files included, can move into
their packages' own `tests/` directories under the locality work.

- **No new work on the deleted mechanisms.** New features do not add port-block,
  pointer, launcher or preview dependencies.

## Follow-up outside this plan

- **`Settings` captures about 25 other variables at import**, which is why the
  test fixture must still run before any project import. Making it lazy is the
  same fix as slice 2's for the home, applied to configuration, and is its own
  work.
- **The older identity channels are retired** (done; see
  [a home's identity lives in its `.env`](../../decisions/2026-10-01-retire-machine-identity-files.md)).
  The `$AVA_HOME/machine_*`, `gateway_url` and `memory_remote` files are no longer
  read, and `ava start` admits a home only through its intent. A home that still has the
  files gets their values into its `.env` by a hand step before the first start of the
  code that contains the change, and the files deleted.
- **The permissions helper's launchd label is still per home**
  (`com.ava.permissions-helper.<slug>`, `helper_job_label` in
  `services/permissions_helper/launchd_job.py`), while every other OS job label is
  fixed and gated on the default home. Slice 4 left it on purpose: on macOS a
  different label is a different background item, which can ask the user to
  approve it again, and `unregister_helper` checks the plist it retires against
  the home's own label and socket, so a fixed label also means reworking that
  check. It is its own change, taken with the approval cost in view.
- **`ava --help` with no subcommand is refused from a checkout that is not the
  home's own `source`.** The guard in `cli/preflight.py` (`require_own_checkout`)
  matches the verb path against the read-only allowlist, and an empty path
  matches none of its entries. A minor flaw: nothing is changed or exposed, the
  help text just is not printed.

## Not yet known

- Whether `ava start` runs end to end inside a Tart macOS guest. The signed
  helper chain and the grants of a cloned image are answered (Experiment B in
  [verification boundaries](verification-boundaries.md#experiment-b-clone-inheritance-outside-the-repository-run-2026-10-01)):
  a clone keeps the helper identity and both grants, with no approval at clone time.
- Whether any agent workflow still depends on a worktree cluster; the
  self-development skill does not require one.
- Whether the impersonation preview can run in a container with the CLI logins
  it needs.
