---
name: ops
description: "Operates Ava cluster lifecycle and runtime sessions. Use when starting, stopping, enrolling, updating, releasing, or inspecting a cluster."
---

# Ava Ops — Cluster Lifecycle & Maintenance

This sub-skill covers the operational verbs: starting, stopping, updating,
and understanding the cluster's runtime layout.

## Cluster / Unit / Machine Model

Hold this mental model once and the commands stop looking arbitrary.

- A **cluster** is one logical deployment. It owns its **own** Postgres + Redis
  instance (under its `$AVA_HOME`, on per-cluster ports), one outward gateway,
  one block of ports. Your peers and you live inside one cluster; you all see the
  same database and message bus.
- A **unit** is one install on one box, under its own home directory. A unit
  carries a set of **capabilities**: `gateway` (owns the data plane + the HTTP
  gateway) and/or `agent-runner` (runs agent processes). A single box usually
  carries *both*.
- A **machine** is a named box in the cluster. Most clusters are a single box
  (it carries both capabilities). The network posture is uniform — a single box
  is just the special case where the only reachable address is loopback, not a
  separate mode you switch on. Each machine carries a free-text description of
  what it is for, so when a task needs a capability your box lacks you read the
  other machines' descriptions (`ava.agents.list_machines()`) and spawn a peer
  there.

## Data Plane Discipline

Each cluster owns its **own** Postgres + Redis instance under its `$AVA_HOME`
(on per-cluster ports), driven directly via `pg_ctl` / `redis-server` — not
shared brew/systemd services, and not one instance partitioned by database name.
`ava start` ensures this cluster's pair is up (skip-if-running); `ava stop` tears
it down. Because `ava stop` (gateway role) takes this cluster's data plane down,
it stops the whole host's cluster; a worktree owns no cluster of its own.

Redis auth has two users, and confusing them is what turns an auth error into a
self-inflicted outage:

- `default` (admin) — its `requirepass` is the gateway-only
  `AVA_REDIS_ADMIN_PASSWORD`. It exists only to provision the runtime ACL user
  and run admin probes.
- the cluster's **ACL user** — your runtime identity (mirrors the per-cluster
  Postgres role), authenticating with its separate runtime password embedded in
  `AVA_REDIS_URL` and scoped to this cluster's keys + channels. It is
  **runtime-provisioned, not persisted**: only `requirepass` lives in
  `redis.conf`, so a bare `redis-server` restart brings redis back with the ACL user *gone*, and runtime connections fail with
  WRONGPASS / NOPERM until it is re-created.

So an auth error from inside your cluster is *the dropped ACL user*, not a
rotated password. The repair is `ava start` — it re-affirms the ACL user
idempotently. **Never restart redis (or Postgres) at the OS level to "fix" a
cluster auth error**: it drops the very ACL user that was failing, and on the
prod box it is a production outage. If `ava start` does not converge, escalate
instead of experimenting on the data plane.

## Start / Stop / Status

```bash
ava init ...  # once per home: record its identity (see `ava init --help`); starts nothing
ava start     # provision owned storage on the first start, and wait for the selected
              # root services to become ready.
ava stop      # normal drain, then full local stop; durable data and agent IDs survive
              # --keep-infra / --keep-service retain resources; --force is explicit
ava start     # after a stop, restore services and resume after readiness
ava restart   # stop then start in one command; keeps private pg+redis and the browser, closes terminals
ava status    # check status (includes the pg/redis view)
```

For coordinated downtime and recovery, use the shared
`docs/conventions/operations/graceful-maintenance.md` in the Ava source checkout.

**Bring-up ordering is strict.** Agent processes are never started directly —
they are always created through the gateway (`POST /api/agents`, which
`ava.agents.spawn` / the frontend / `scripts/entrypoints/agent.py` all share). So
**start the gateway first, then start agents**; a spawn issued before the
gateway is up has nowhere to land.

### Cluster sub-commands

```bash
ava cluster status                    # full multi-machine roster
ava cluster destroy                   # decommission this host's cluster: stop + deregister its
                                      # OS-scheduled jobs + mark the home detached. Needs a
                                      # terminal; you type the home path (no flag skips it)
                                      # add --drop-db to also remove its pg/redis data dirs
```

### Split deployments

A pure agent-runner on another box **enrolls** into an existing cluster instead
of birthing one of its own — it inherits the cluster's identity (db / redis /
channels) from the gateway:

```bash
# on the gateway: seal the unit's database capability (prints its key once)
ava cluster db-authority issue-unit --machine <NAME> --home <runner $AVA_HOME> --out <NAME>.bundle
# on the runner, with the bundle carried over:
printf 'Capability transport key: ' >&2
IFS= read -rs AVA_DB_CAPABILITY_KEY
printf '\n' >&2
export AVA_DB_CAPABILITY_KEY
ava init --serve-agent-runner --no-serve-gateway --gateway-url <URL> \
  --machine-name <NAME> --machine-host <HOST> --db-capability <NAME>.bundle
unset AVA_DB_CAPABILITY_KEY
ava start
```

The sealed bundle carries the active write generation's runner login (it
inherits the least-privilege `ava_runner` group) and machine API token, bound
to this machine and home and installed into the runner's private
`$AVA_HOME/db-authority/`. `ava init` presents that API token to the gateway's
authenticated `/api/bootstrap`, which returns the cluster's configuration (the
credential-free database endpoint, the Redis URL with its runtime ACL password,
channels). The runner never holds the gateway's human cluster secret; the
gateway's schema owner never logs in, and its Redis-admin credential stays
gateway-local. `--machine-host` is the runner's own reachable address (how the
gateway dials back to its ops server) and is **required**. The runner starts no
gateway process of its own; it needs network reachability to the gateway and
its capability bundle. A later bundle (a fresh expiry, a rotated telemetry token) is
installed on the stopped runner with `ava cluster db-authority install-unit
<NAME>.bundle` before `ava start`.

## Update and recover

A production cluster runs every unit from its own source checkout and is updated
by stopping every unit, switching every checkout and starting again:
`python -m cli.fleet_update down` and `up`, attended and idempotent per half (the
runbook's "Updating a networked cluster in source mode"). A result from anywhere
else does not replace CI, review or operator authorization for production.

Related local commands:

- `ava restart` restarts this home's application through its ordinary lifecycle.
- `ava converge` applies development host wiring.
- `ava status` and `ava cluster status` provide observations, not permission to
  update the cluster.

## Update safety discipline

- **Merge is not runtime health.** Require repository review and CI, a fixed
  target, verified recovery evidence and operator authorization for production.
  Verify the selected service roster and representative agent progress after
  activation. Skipped checks are not successful checks.
- **Retain executing code.** Never change the checkout, interpreter or libraries
  underneath a serving process: `down` stops every unit before it switches a
  checkout.
- **Recover by rerunning the half.** After a failure, fix the cause and rerun
  the whole half; both are idempotent. Unknown custody or a hold that is not a
  completed stop needs diagnosis, not raw signals or hand-edited state.
- **Treat first adoption as a separate cutover.** A new mechanism cannot
  protect the legacy deployment that introduces it. Inspect the currently
  running implementation and follow the approved explicit cutover procedure.
  Respect any explicit CI-only or no-local-cluster constraint.

## Resource Oversight (the SRE loop)

Read [resource diagnosis](references/resources.md) when investigating host or
data-plane pressure. Identify the consumer and compare deployment thresholds
before acting; readings alone do not authorize a production change.

## Sessions

Read [sessions](references/sessions.md) when inspecting session names, logs,
environment forwarding, or shell survival. Agent termination alone does not
end its shell sessions; cluster stop/restart can close them.
