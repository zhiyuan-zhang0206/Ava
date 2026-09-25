# Preview cluster

The **preview** cluster is the pre-release validation environment for `main`:
a full Ava cluster running the commit that is about to be promoted, so a
release is exercised end-to-end before production sees it.

## Preview an unmerged local branch

On macOS or Linux, from a developer checkout:

```bash
python3 -m scripts.preview.local run --ref my-local-branch
python3 -m scripts.preview.local run --ref origin/my-remote-branch --keep
python3 -m scripts.preview.local check ~/.ava-previews/<run-directory>
python3 -m scripts.preview.local stop ~/.ava-previews/<run-directory>
```

`--repo /path/to/repo` selects another local repository; fetch remote branches
there first. A ref resolves once to an exact commit. Uncommitted working files
are not included, and a running preview does not follow later branch movement.
Repeat `run --ref ...` to test the next commit. Neither merge nor green CI is a
prerequisite. The target revision must support the existing worktree installer,
core service roster and scripted `message_flow` scenario; incompatible revisions
fail visibly rather than silently switching to another revision.

The standard-library controller creates a detached worktree and private home
under a new `~/.ava-previews/<run-directory>` (override the parent with `--root`).
It pins Python 3.12, calls the existing `install.sh --worktree --no-seed`, installs
frontend dependencies, then runs that checkout's own `ava start` and `ava stop`.
Git, uv, Node/npm and the normal native install prerequisites must be available.
Run trusted branches: home isolation is not a sandbox for arbitrary source code.

This bounded profile starts gateway, frontend, ops and agent-host with private
Postgres, Redis, PgBouncer and allocated ports. It disables OS jobs, GUI handover,
desktop/browser services and remote memory synchronization. The launch environment
omits inherited Ava settings, Python overrides and provider credentials; it never
seeds a production `.env`. Use the printed **frontend app URL** directly; the
OS-managed gate is deliberately absent in this profile.

Verification requires identity probes for all four services, a frontend HTTP
response and a real agent turn: a scripted model asks `execute_code` to run
`print(1 + 2)`, and the committed timeline must contain exactly that code and the
output body `3`. The scripted reply alone cannot pass. This checks source boot
and execution; it does not prove installed release update/rollback, multiple
machines, browser rendering, real model providers or production readiness.
CI, merge and production promotion gates remain separate.

By default, success and failure both stop the preview. `--keep` retains only a
successful run for inspection; use `check` and `stop` with its printed directory.
Foreground build processes are reaped on timeout or interruption. An install
that failed before writing `.env` uses a narrowly scoped native-daemon cleanup.
SIGKILL/power loss cannot execute cleanup: run `stop` on the recorded directory
when resuming. Concurrent operations on one run are refused.

`run.json` records the requested ref, resolved commit, controller/adapter hashes,
verification scope, each phase's command, log, duration and result, and cleanup
status. `config.json`, `check.json`, `smoke.json` and `cleanup.json` carry the
resolved ports, probes, timeline and closure evidence. Registry ports are released
only after sessions, owned processes and listeners are confirmed gone. Source,
dependencies, data and logs remain on disk for diagnosis; after confirmed cleanup,
remove the worktree with `git worktree remove --force <run-directory>/source` and
remove the retained run directory when its evidence is no longer needed.

## Where it lives

| Fact | Value |
|---|---|
| Host | its own machine — **not** the production gateway host. Which machine, and how it is reached, is deployment inventory, not repo content ([`dev-setup.md`](../../conventions/dev-setup.md)) |
| Home (`$AVA_HOME`) | `~/.ava-preview` |
| Checkout | `~/.ava-preview/source`, anchored to that home by its `.ava_home` pointer |
| Data plane | its **own** Postgres + Redis + PgBouncer under `~/.ava-preview`, on its own port block |
| Auth | preview runs **with** a cluster secret, so every gateway route except `/api/health` and `/api/auth/*` requires `Authorization: Bearer $AVA_CLUSTER_SECRET` |

Preview shares nothing with production — not a Postgres instance, not a Redis
instance, not a port, not a session. Isolation is home-directory isolation
([AGENTS.md → Running](../../AGENTS.md)): there is no box-level Postgres or
Redis for two clusters to collide in, and no box-level admin credential either
— a cluster's Redis instance is single-tenant and its `requirepass` **is** that
cluster's secret.

> History, not current advice: before the per-cluster data plane, every cluster
> on a host shared one Postgres and one Redis, and restarting the box's Redis to
> "fix" one cluster's wrong password took every cluster on that box down with it
> (2026-07-14). The shared instance that made that possible no longer exists.

## Operate it

Always through the cluster's **own** `ava`. A bare `ava` on that host's PATH
belongs to a different checkout and acts on a different home, and
`AVA_HOME=~/.ava-preview` does not redirect it — the boot refuses an env var
that contradicts the checkout's own claim
(`shared/dotenv_boot.py:_assert_env_agrees_with_checkout`).

```bash
cd ~/.ava-preview/source
.venv/bin/ava status           # sessions + probes + this cluster's pg/redis view
.venv/bin/ava start            # pure bring-up; ensures preview's own pg/redis is up (skip-if-running)
.venv/bin/ava cluster update   # pull main -> uv sync -> migrate -> restart, preview only
```

A cron registered for this home runs `ava cluster health-probe --auto-rollback`,
so a cluster that stays unhealthy for `--threshold` consecutive probes (default
3) rolls itself back with no operator in the loop.

## Blast radius

| Command (as `~/.ava-preview/source/.venv/bin/ava`) | Touches |
|---|---|
| `start` / `stop` / `restart` | preview only, **including its own pg/redis** — `stop` takes preview's data plane down with it |
| `cluster update` / `cluster rollback` | preview only: its checkout, its database, its sessions |
| `cluster down --path ~/.ava-preview` | stops preview's sessions; its pg/redis instance and its registry slot stay up |
| `cluster destroy --path ~/.ava-preview` | the above, plus frees its port block and deregisters its OS-scheduled jobs (`--drop-db` also drops its data) |

Nothing in that table can reach production. What still can: the host itself
(reboot, disk, network), and anything run against `~/.ava` on the same machine —
that is a **different** cluster, not preview.

## Validate a release

`ava cluster update` rolls the code; validation is a separate, agent-driven
suite:

```bash
cd ~/.ava-preview/source
bash scripts/preview/validate.sh        # one validation agent runs validate-tasks/suite.md
bash scripts/preview/spawn-samples.sh   # optional: mock agents from mock-tasks/, to eyeball FleetView
```

Both resolve the repo from their own location and dial the gateway through
`shared/machine.py:gateway_api_base` + `shared/machine.py:gateway_auth_headers`,
so they carry no hardcoded path or port and work whether or not the cluster has
a secret. `validate.sh` returns once the task is delivered; the agent writes its
report to `$AVA_HOME/preview-validation-report.md` — outside the checkout, so a
validation run can never dirty the git tree — and notifies when it is done.

## Mac updater definition proof

From the candidate worktree, explicitly select an installed non-production home
and run `python -m scripts.preview.prove_mac_bootstrap_jobs` with that home's
`AVA_HOME` (and `AVA_HOME_OVERRIDE=1` only for this cross-checkout preview proof).
The script creates unique unloaded `/usr/bin/true` definitions, crashes its own
child after one atomic custody move, and restores from serialized originals.
It never loads or signals a job. Its `result.json` under the chosen home's run
directory names the proved cases and explicitly excludes complete image-hop
proof. Existing loaded job definitions remain outside this primitive's authority.
