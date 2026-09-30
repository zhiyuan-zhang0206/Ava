# One cluster per host: the deletion plan

**Status: decided, in progress.** The ruling and its reasons are in
[one cluster per host](../../decisions/2026-09-30-one-cluster-per-host.md). This
doc is the order of work: six slices, each its own PR with its docs and tests.

## Boundary

A host runs one cluster at `~/.ava`. Anything that verifies something runs in a
container, a VM or a Tart macOS VM, where it is that boundary's one cluster.
In scope: the mechanisms that keep several clusters apart on one host, and the
way a process learns its home. Out of scope: the release path and PITR shell
(owned by the updater work), the docs and tests relocation (owned by the
locality work), and the data plane itself.

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

| # | Delete | Main files | Risk |
|---|---|---|---|
| 1 | The native local preview controller, its mock and validation tasks, its tests and the lint allowlist entry | `scripts/preview/**` (15 files), six of the seven tests in `tests/lifecycle/preview/` (`test_visual_gate.py` stays), `scripts/lint/no_os_environ.py`, docs and skills that name it | Leaf: one importer outside itself, no production path |
| 2 | `ava start --worktree`, the `.ava_home` pointer, checkout binding and lock, the env-versus-pointer contradiction check, `AVA_HOME_OVERRIDE`, the unanchored scratch home. The home becomes `AVA_HOME` or `~/.ava`, read when called; the import-time users of the home become lazy; only `~/.ava/source` may start or stop `~/.ava`; hooks and tools set a scratch home | `base/host/env/dotenv_boot.py`, `base/paths`, `cli/start_identity.py`, `cli/start_intent.py`, `cli/parsers/host.py`, `tests/fixtures/env_bootstrap.py`, the hook configuration | Start path, and every tool that relied on the scratch home. Existing intents carry `worktree` |
| 3 | Port-block allocation and cross-cluster preflight; new homes use the fixed port table; a home keeps reading its recorded ports; tests keep ports of their own | `base/cluster/{ports,port_preflight,derive,ownership}.py`, `base/host/env/port_block.py`, `cli/commands/converge/{port_preflight,health_preflight,redis_bridge}.py`, `cli/commands/data_plane/{bringup,cluster_instance,pgbouncer}.py`, `cli/preflight.py`, and the consumers of `record_*_port` (about 30 files) | Highest: data-plane bring-up. Production records must read unchanged. Starts with a read-only design pass on how tests get their ports |
| 4 | The bare-`ava` launcher routing, the per-home CLI link, `AVA_HOST_STATE_DIR` (host state is the home), per-home OS job slugs; `ava` on PATH becomes a direct link to the production source CLI | `scripts/ava-launcher.sh`, `cli/commands/converge/{_steps,host}.py`, `base/config/general.py`, `base/paths`, OS job labels, `tests/base/test_bare_ava.py` | Host wiring. Converge rewrites the PATH link on the next start |
| 5 | The one-step birth inside `ava start`: `ava init` takes the first-start flags (13 of `start`'s 17), `start` keeps service selection and fails fast on an uninitialized home | `cli/parsers/host.py`, `cli/start_intent.py`, `cli/start_identity.py`, `cli/commands/lifecycle/start.py` | Small once slices 2 to 4 have removed `--worktree` and the port negotiation |
| 6 | Nothing is deleted: the container and Tart recipes that replace the native preview (a Linux image running the normal stack, a Tart base image with its one-time grants) | new, design first | Separate design; slices 1 to 5 do not wait for it |

Slice 1 goes first: a pure deletion that removes the largest consumer of
everything after it. Slice 2 and the design pass of slice 3 run in parallel;
slice 3 is implemented against whatever of slice 2 has merged. Slice 4 follows
slice 2, and slice 5 follows 2 to 4. Slice 6 is designed in parallel.

## Coordination

- **Updater work.** It touches the same start, preflight and launch-record files
  while deleting the release path. Each slice starts from a freshly fetched
  `main` and is announced to that session before it opens.
- **Locality work.** Package docs already live under each package's `docs/`, so
  a slice edits the node where `main` has it. Unit tests are moving into each
  package's own `tests/` directory; the tests that exercise these mechanisms stay
  where they are until the slice that deletes or rewrites them has merged. A
  slice that finds a moved file follows the move instead of restoring the old
  path, and adds, renames or deletes test files as usual.
- **Release point for that work.** Slice 4 is the last to touch `base/cluster`
  and `base/host/env`; slice 5 touches `cli/` only and slice 6 adds new files.
  The pinned `tests/base` files can be moved once slice 4 has merged.
- **No new work on the deleted mechanisms.** New features do not add port-block,
  pointer, launcher or preview dependencies from the day this lands.

## Follow-up outside this plan

`Settings` captures about 25 other variables at import, which is why the test
fixture must still run before any project import. Making it lazy is the same
fix as slice 2's for the home, applied to configuration, and is its own work.

## Not yet known

- Whether the signed helper chain starts in a fresh Tart macOS VM with no extra
  approval, and whether a cloned base image keeps its grants.
- Whether any agent workflow still depends on a worktree cluster; the
  self-development skill does not require one.
- Whether the impersonation preview can run in a container with the CLI logins
  it needs.
