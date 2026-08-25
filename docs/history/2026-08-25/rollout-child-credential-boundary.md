# Rollout child credential boundary

## Context

The gateway rollout deliberately keeps its orchestration interpreter alive
across the checkout, while `ava start` runs the new tree in a child process. A
one-time data-plane credential split in that child updated Postgres, PgBouncer,
Redis, and `$AVA_HOME/.env`. The update could not flow back into the surviving
parent process, which then tried to persist the cluster pin with its old
PgBouncer password. That SASL failure also reached the parent's recovery and
outcome-recording paths, leaving the pin behind the checked-out tree and
triggering watchdog convergence in the wrong direction.

## Decision

Returning from the fresh `ava start` is an explicit credential boundary. The
parent re-reads the credential-bearing data-plane values from the unit `.env`,
updates its process environment, and refreshes the existing Settings singleton
before it performs any subsequent pin or recovery database operation. The
refresh lives in a `finally` around the child wait, so a SIGINT that enters
rollback after the child already changed credentials uses the new values too.

The boundary is intentionally narrow: general configuration still has no live
reload contract and belongs to the fresh service processes. Re-executing the
whole orchestrator was rejected because it would have to reconstruct the live
deploy lease, pre-resolved runner URLs, paused-host set, and recovery snapshot
across a process handoff.
