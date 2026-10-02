# The roster read renders from heartbeat snapshots, and the gateway keeps no failure memory

## Context

`/api/cluster/roster`, `/api/cluster/machines` and the extensions inventory dialed every
agent-runner on each request. To keep a dead host from stalling the page, the gateway
process kept a failure memory (per-host consecutive failures, a shared backoff, a shorter
fast-fail budget) and ran a detached recovery thread that re-dialed hosts the read had
skipped. That state was process-local: a gateway restart forgot it, two requests raced on
it, and the inventory aggregate shared it with the roster so one view's failures
degraded the other.

## Decision

The heartbeat liveness pass already dials every rollout target once a minute. It now
dials the union of rollout targets and unpaused agent-runners and writes one
`machine_status_snapshot` row per host (`observed_at`, `reachable`,
`consecutive_failures`, the last `ClusterStatus` payload and when it was taken). Judging
and alerting stay with the rollout targets.

- The default roster read renders from snapshots no older than 180 s. A host with a
  missing or stale snapshot is dialed inline; a host whose last attempt failed is
  rendered offline once the failure count reaches the machine-offline threshold.
- `?fresh=true` dials everything. `ava cluster status` and `cli.fleet_update` use it:
  they decide what to do from the answer.
- `MachineStatus.observed_at` says how old the rendered status is.
- The gateway keeps no failure memory and runs no recovery thread. The three
  `status_probe_*` timing settings are removed.
- The pass runs at heartbeat start instead of waiting one interval, so a fresh heartbeat
  does not leave the snapshots stale for a full minute.

## Alternatives rejected

- **Persist the failure memory in a table the gateway writes.** Keeps the reader
  mutating state and the race between concurrent requests.
- **Keep dialing on every read, only move the backoff to the DB.** Page latency still
  scales with dead hosts.
- **Show the data age in the UI now.** Needs new locale strings; the field is in the API.
