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

## Update: durable handoff and predecessor gate

Refreshing from `.env` alone was insufficient when the child was interrupted
between changing an external service and committing that file. The split now
writes one private, atomically replaced v1 journal before its first mutation.
Redis persists the new admin password with `CONFIG REWRITE`; Postgres, Redis ACL,
PgBouncer, `.env`, and process settings replay the same frozen values until the
journal can be removed. Native bring-up consults the journal when current
credentials fail, and replay completes before migrations open their first DB
client.

The parent sends a one-shot `AVA_ROLLOUT_PARENT_CREDENTIAL_HANDOFF=v1` marker in
the fresh child's copied environment and consumes the resulting journal before
refreshing its data-plane settings. A child reached through an older parent has
no marker; the executing deploy lease identifies it as a compatibility rollout,
so it converges services but refuses to begin a new legacy credential split.
This makes installation a two-pass protocol: first install the handoff-capable
parent without rotating credentials, then permit the transition from an
ordinary start or a later versioned rollout.

Child completion, interruption, and adoption failure are separate outcomes.
Every outcome adopts or replays first; an interrupted/failed child then enters
the same last-known-good recovery path. This prevents an adoption exception
from masking the original start failure and prevents SIGINT recovery from using
the pre-transition password.
