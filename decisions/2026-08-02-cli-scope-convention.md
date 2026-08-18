# CLI scope convention: [host] vs [cluster]

Date: 2026-08-02
Status: accepted

## The problem

The CLI's command surface mixed two scopes under one grammar. `ava restart`
restarts only this host, `ava cluster restart` bounces every host; `ava update`
was a top-level verb that orchestrated the WHOLE cluster (the gateway runs a
three-phase rollout and fans out to every agent-runner) — the only top-level
verb with cluster semantics. `ava status` was host-local while `ava cluster
status` was the fleet roster. There was no stated rule; users and agents had to
infer scope per verb, and the inference broke (`update` looks host-local but
isn't).

## The convention

Every command that operates on the whole cluster is written under the
`cluster` namespace: `ava cluster status / restart / update / recover /
rollback / ls / down / destroy / health-probe / cron-* / watchdog-*`.
Every other verb is host-local: `ava start / stop / restart / status /
converge` act on this machine only.

The `[host]` / `[cluster]` markers in `--help` make the scope explicit on the
surface:

```
ava restart         # [host]    — this host's services
ava cluster restart # [cluster] — every host's services
ava cluster update  # [cluster] — whole-cluster rollout (was `ava update`)
```

## Why update is cluster-scoped

Code versions must be consistent across the cluster (the pin mechanism
converges every node to one commit), so "update" is inherently a cluster
operation: there is no legitimate "update just this host" that leaves the
cluster consistent. Process operations (start/stop/restart) are legitimately
host-local — bouncing one machine is a valid ops action — so they default to
host scope and need the explicit `cluster` prefix for the fleet-wide form.

`ava update` predates multi-machine orchestration (it was a single-host verb)
and silently became cluster-wide when the fleet arrived. The rename to
`ava cluster update` (2026-08-02, PR #1234) makes the surface match the
convention; the top-level `update` verb is removed with no alias (user ruling:
no backward compatibility for a command whose scope was misleading).

## What this means for new verbs

- A new verb that touches one host only: top-level, mark `[host]`.
- A new verb that coordinates hosts or reads fleet state: under `cluster`,
  mark `[cluster]`.
- A verb whose scope is neither (agents, config, presets, schedules, plugins,
  skill, mcp, memory) stays top-level without a scope marker.

## History

- 2026-08-02: convention written; `ava update` → `ava cluster update`
  (PR #1234); help markers added (PR of this doc).
