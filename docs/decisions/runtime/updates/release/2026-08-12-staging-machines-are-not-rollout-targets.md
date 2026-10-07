# Staging machines are registered and visible, but never rollout targets

## Context

A staging VM (`ava-staging-01`) runs a full Ava unit — gateway + agent-runner —
so a deploy workflow can smoke-test against a local gateway before production.
For the gateway to register itself and the roster to show the host, the machine
must run `ava start`, which clears its `stopped_at` latch. Before this change,
`stopped_at` was the only thing keeping a host out of `list_agent_runners()` /
the rollout fan-out, so a staging host that ran `ava start` became a rollout
target by accident — with the real production pin, migrations and all.

The earlier latch (a `stopped_at` marker set by `ava stop` on the staging host,
Task #1130) was a workaround: `register_self()` clears it at every boot, so it
could not survive the very thing a staging host must do (run `ava start`).

## Decision

`machines` gets an operator-set `is_staging` boolean (default false):

- `list_agent_runners()` and `list_stopped_agent_runners()` exclude
  `is_staging` rows — a staging host is never a rollout target, regardless of
  its `stopped_at` latch.
- The flag is written **only** by the operator: `ava cluster mark-staging NAME`
  / `unmark-staging NAME` (thin client → `POST /api/cluster/machines/{name}/staging`).
  `register_self`, the ops daemon and `_recompute_machine_row` never touch it,
  so `ava start` on a staging host keeps it excluded.
- Staging hosts stay roster-visible: `MachineStatus` / the status panel / the
  CLI roster carry the flag, rendered as a `(staging)` marker. Agents see it
  too (`AgentMachineRow.is_staging`) so a peer can tell a staging host from a
  production target before enrolling against it.

## Consequences

- A staging host can run `ava start` freely; the flag, not the latch, is the
  exclusion.
- The rollout's "N of M KNOWN hosts" counts only non-staging rows
  (`list_stopped_agent_runners` keeps the same predicate, so the two lists
  still partition the non-staging agent-runner population).
- The flag is a latch, not a liveness signal: nothing re-derives it from probe
  results, and `mark-staging` on a missing name is a 404.
- Decommissioning a staging VM still goes through the machines DELETE endpoint;
  `is_staging` does not gate that path.
