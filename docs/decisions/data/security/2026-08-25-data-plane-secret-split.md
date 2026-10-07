# Data-plane secret split

## Decision

Separate control-plane authentication from data-plane authority. The cluster
bearer authenticates only gateway API, `/ops`, bootstrap, and machine
registration. The gateway keeps independent Postgres owner and Redis default-user
passwords. Runners use two independent least-privilege credentials: the existing
Postgres runner role and the Redis ACL user.

## Why

The previous single bearer was also the owner Postgres password and Redis admin
password. Every runner necessarily held it to bootstrap, so a compromised agent
could bypass the runner role's database grants and the Redis ACL's dangerous-command
restriction.

## Consequences

Agent launch and bootstrap project only the runner DB URL and Redis runtime URL.
Gateway start upgrades legacy authenticated homes after migrations by minting the
three new data-plane values; empty-bearer homes remain unauthenticated. Bearer
rotation is an emergency control-plane procedure, while routine password rotation
uses separate admin and runner scopes with persisted recovery state.
