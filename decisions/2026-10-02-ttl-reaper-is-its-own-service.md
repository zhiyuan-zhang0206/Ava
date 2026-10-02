# The TTL reaper is its own service with two resident loops

## Context

The reaper was a free-floating `asyncio` task in the gateway process: one loop ran ten
phases in order (pages, shells, browser sessions, notices, impersonation, stale work
failures, three slow maintenance phases). The shell phase dials every runner and was
serial, up to five attempts per dead machine, so one unreachable machine delayed every
phase behind it and the gateway's own shutdown waited on it. The cadence of the three
slow phases was a module-level monotonic stamp, so every gateway restart ran all three
at once. Stopping the reaper meant restarting the gateway.

## Decision

`ttl-reaper` is a root unit on the gateway side (`services/ttl_reaper`, health port slot
8121). Two resident sequential loops are owned by one `TaskGroup` in the service's main
function; a loop that raises ends the process and the supervisor restarts it.

- `sweep`: the database-only phases, plus the slow phases.
- `remote`: shell kills and stale work-failure redelivery, the two phases that call
  other machines or processes.

The loops are split by failure domain, not by phase: a wedged machine holds up the
`remote` round, never a page expiry. Within the `remote` round the expired shells are
grouped by home machine, machines run concurrently and one machine's rows in order. Every
dispatch runs under a deadline sized from the RPC client's own retry budget, and every
redelivery under twice the client's worst dispatch; a cut leaves the row for the next
round. The cadence clocks of the slow phases live in `maintenance_state` (one row per
phase), claimed with one statement that checks the clock and stamps it, like the
watchdog's attempt clocks.

The gateway keeps no reaper: its lifespan no longer starts or stops one.

## Alternatives rejected

- **One loop with all phases.** The shell phase's remote calls would still hold up the
  database-only reclaims.
- **One service per phase group beyond two loops.** More pidfiles, health ports and pool
  connections for a failure domain the phases share (the same tables).
- **Keeping the monotonic stamps.** A restart re-runs the daily prune and both hourly
  scans at once.
- **Writing an `interrupted` marker before the kill** (closes the gap where a kill is
  dispatched but the service is cut before the row is deleted, so the interruption notice
  is lost). Not done here; the next round's kill answers `absent` and stays silent.

## Consequences

- One migration (`maintenance_state`), one new fixed-port slot (`ttl_reaper`, which every
  gateway home's start intent must carry, see the runbook's release steps) and a root
  generation change roll this out.
- The service imports `gateway.routers.work_failed` for the redelivery path, which pulls
  the router module into the process; moving that path out of the router is a separate
  change.
- Stopping the reaper no longer restarts the gateway; a stop cancels in-flight
  dispatches, so a shell killed but not yet deleted is re-selected by the next start.
- A persistent bug in one phase now restarts the service instead of being logged and
  retried next interval; the supervisor's restart backoff bounds the loop.
