# PG-backup scheduler

## Context

The daily dump ran as the final watchdog check. Its bounded but long-running
subprocess could delay every other supervision decision for up to an hour.

## Decision

`pg-backup` is now a gateway-owned scheduler daemon declared in the service
roster. It retains the existing due-check, cluster-clock scheduling, dump, and
prune logic, but owns retry and health state. The watchdog only verifies the
daemon's identity and health; it restarts a scheduler whose last successful
backup becomes overdue.

This keeps a dump's timeout isolated from the watchdog while preserving
catch-up after a missed schedule. A separate general scheduling abstraction was
not added: one supervised daemon is sufficient for the only concrete job and
does not pre-commit the remaining scheduling design.
