# Preview cluster

The **preview** cluster is the pre-release validation environment for `main`:
a full Ava cluster running the commit that is about to be promoted, so a
release is exercised end-to-end before production sees it.

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
