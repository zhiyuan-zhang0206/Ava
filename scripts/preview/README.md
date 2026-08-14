# Preview Cluster

The **preview** cluster is the pre-release validation environment for `main`.
It runs **co-located with the production `main` cluster on the gateway host**
(the WSL physical-isolation plan in `future/infra/preview-cluster.md` is
not the current reality — that host is offline). Co-location is exactly why the
discipline below exists: most of the infrastructure you can see from inside the
preview cluster is *shared with production*.

## What is shared, what is yours

Per the current shared-instance model (`ava-guide` → `ops/SKILL.md`), a box runs
**one** Postgres and **one** Redis as native services, shared by every cluster on
it. What belongs to one cluster is logical: a database name (`ava_preview`), a
Redis logical-DB index + channel prefix, a port block, session homes, and
`$AVA_HOME` (`~/.ava-preview`).

| Layer | Scope | Restart blast radius |
|---|---|---|
| `brew services restart redis` / `postgresql@17` | **the whole box — every cluster, including prod `main`** | outage for all clusters |
| `ava stop` / `ava start` (from preview's own checkout) | the preview cluster only (`ava stop` on a gateway role still touches shared pg/redis — read its prompt) | preview only |
| `ava-*` sessions on preview's own home | one preview service | that service |

**Never restart a shared service to fix a cluster-level problem.** On
2026-07-14 an agent ran `brew services restart redis` to "fix" a preview auth
error; the restart wiped every cluster's in-memory Redis ACL user and took
down all of production `main` (agents crashed, SSE dead) for ~15 minutes. The
auth error it was fixing was a wrong password, not a broken server — see
"Redis identities" below. (The gateway watchdog now re-affirms the ACL within
60s, but that is damage control, not permission.)

## Redis identities — the two-layer model

Redis auth on a shared box has exactly two layers. Confusing them produces
`WRONGPASS invalid username-password pair`, which is **your credentials being
wrong, not the server's password having rotated**:

| Identity | Username | Password | Used for |
|---|---|---|---|
| Box admin | `default` | box-level admin secret (`~/.ava/redis_admin_secret`, = `requirepass` in redis.conf) | provisioning per-cluster ACL users; never a cluster's runtime identity |
| Cluster runtime | `ava_preview` | the **preview cluster secret** (`AVA_CLUSTER_SECRET` in `~/.ava-preview/.env`) | everything the cluster does at runtime |

A cluster secret is never the admin password. If `default` + cluster-secret
fails, that is expected. You never need to authenticate as admin by hand:
`ava start` provisions the ACL user through the admin identity itself.

## Operating the cluster

All provisioning and auth repair goes through the one idempotent verb — it
creates/affirms the DB role, the Redis ACL user, and the session stack, and it is
safe to re-run:

```bash
# Run from preview's OWN checkout — identity is the home path, so the install
# births the cluster at ~/.ava-preview (secret minted into its .env) and anchors
# this checkout to it via the .ava_home pointer; `ava start` is then a pure
# bring-up with no identity flags.
cd ~/.ava-preview/source
scripts/install.sh --worktree --path ~/.ava-preview
.venv/bin/ava start --machine-name <host>
```

Status / teardown:

```bash
AVA_HOME=~/.ava-preview ava status
ava cluster status
ava cluster down --path ~/.ava-preview      # stop it, keep its slot; shared pg/redis untouched
ava cluster destroy --path ~/.ava-preview --drop-db   # free the slot too
```

If auth to Redis fails from inside preview: check which identity you are using
against the table above, then re-run `ava start` (it re-affirms the ACL user).
If that does not converge, **stop and escalate to the user** — do not
experiment on shared services.

## Known drift (read before touching the data plane)

The preview `.env` currently records a per-cluster data plane
(`AVA_DB_URL` → :18043, `AVA_REDIS_URL` → :18044) per the *planned*
embedded-per-cluster-data-plane design. Reality: preview Postgres does run as
its own instance on 18043, but **no Redis listens on 18044 — preview Redis is
the shared instance on 6379, logical DB 3**. Until the embedded design lands,
treat 6379 as shared-with-prod (all discipline above applies) and expect the
`.env` Redis line to disagree with the wire.

## Validation & samples

```bash
bash scripts/preview/validate.sh        # spawn/terminate/resurrect/fork/messaging/files/shell/web/notices
bash scripts/preview/spawn-samples.sh   # sample agents from scripts/preview/mock-tasks/
```
