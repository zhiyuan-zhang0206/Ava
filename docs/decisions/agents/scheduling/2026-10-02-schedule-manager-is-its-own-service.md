# The schedule manager is its own service; its state lives on the schedule row

## Context

`ScheduleManager` was one background task in the gateway process. The crash backoff and
launch counter, the sessionless clock and the stall-alert flag were process-local dicts, so
a gateway restart gave a crash-looping schedule that had not yet tripped the breaker a
fresh counter and restarted the two-hour alert clock. The API called `sync` on the
in-process object, so start / stop / restart only worked in the process that owned the
manager. Stopping the manager meant restarting the gateway.

## Decision

`schedule-manager` is a root unit on the gateway side (`services/schedule_manager`, health
port slot 8122) with two resident sequential loops under one `TaskGroup`: `reconcile` (the
old tick) and `requests`.

- The backoff and the alert state become columns of the schedule row (`launch_count`,
  `next_launch_at`, `not_live_since`, `stall_alerted_at`). A launch is claimed with one
  conditional UPDATE that checks the count the reconcile read and the backoff deadline, and
  an alert claims `stall_alerted_at` in the statement that finds it due, so a restart
  resumes both.
- The API stops calling the manager. Start / stop / restart / edit / delete upsert a row in
  `schedule_sync_requests` (no foreign key: a delete asks for the orphaned session to be
  killed after its row is gone) and wait up to 8 seconds for it to be consumed; the
  `requests` loop runs the sync and deletes the row only if it is unchanged, so a crash
  between the two runs the sync again, which is idempotent. Log capture needs no manager
  state and stays in the gateway.
- Built-in seeding runs when the service starts. A checkout that does not own the home
  refuses to start the service instead of logging and carrying on.

## Alternatives rejected

- **A wake over Redis instead of a polled queue.** A subscriber loop and a lost-wake
  fallback to build, for a one-second poll of a primary-key table that is empty almost
  always.
- **Keeping the in-memory dicts.** A restart would keep granting fresh counters.
- **Waiting for the sync in the API response unboundedly.** A down service would hang
  every start and stop.

## Consequences

- One migration (the four columns and `schedule_sync_requests`), one new fixed-port slot
  (`schedule_manager`, every gateway home's start intent must carry it, see the runbook's
  release steps) and a root generation change roll this out. The schedule sessions
  themselves are not restarted.
- A start / stop / restart answers within a second or two with the converged status when
  the service is up, and after the 8-second wait with the unconverged status when it is
  not; the request is consumed when the service is next up.
- A persistent bug in one decision now restarts the service instead of being logged every
  five seconds; the supervisor's restart backoff bounds the loop.
- A backend or database failure inside a tick that is not a lost connection ends the
  process; the old loop logged and carried on.
