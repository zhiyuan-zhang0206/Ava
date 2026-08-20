# 0004 — Isolation that one command can undo is a convention, not a boundary

**Date:** 2026-07-14
**Anchors:** The incident predates the 2026-08-18 public-repo cutover, so no
commit, PR, or log from it is reachable from public `main`; this narrative is
reconstructed **(summarized)** from three records that survived it. (1) The
account carried by `scripts/preview/README.md` until PR #179 demoted it — that
README was the only place the story lived. (2) The same day's redis `requirepass`
rotation, recorded in
[`ava-serious-engineering/practices/security`](../.agents/skills/ava-serious-engineering/practices/security/SKILL.md)
("when a rotation happens (redis requirepass, 2026-07-14), record it and
coordinate every consumer"). (3) The guardrail it bought, which names the outage
by date in its own docstring: `services/healthchecks/redis_acl.py`. Surviving
code: `services/watchdog/daemon.py` (check ordering),
`cli/commands/_cluster_instance.py:_ensure_redis_acl`,
`shared/cluster/provision.py:ensure_cluster_redis_acl`. Design record:
[`future/infra/embedded-per-cluster-data-plane.md`](../future/infra/embedded-per-cluster-data-plane.md).

## Summary

An agent hit `WRONGPASS invalid username-password pair` on the preview cluster
and ran `brew services restart redis` to fix it. The credentials were wrong; the
server was fine. Restarting could not have repaired a wrong password, and it did
the one thing that mattered: every cluster's Redis ACL user lived only in that
one shared server's memory, so the restart wiped all of them. Production went
down for ~15 minutes — agents crashed on their next Redis call, SSE died on
attach — while the gateway's own HTTP health stayed green throughout.

Two facts multiplied. A credentials error was read as a server error, which is
the ordinary way that mistake is made. And one box ran one Redis for every
cluster on it, so a preview-scoped remedy had production-scoped reach. The
guardrail is architectural: the
[per-cluster data plane](../future/infra/embedded-per-cluster-data-plane.md)
gives every cluster its own Postgres and Redis under its own `$AVA_HOME`. The
same mistake today restarts one cluster's own server and cannot touch a
neighbour. This incident is the strongest single argument for that design,
because it is the failure that design *eliminates* rather than mitigates.

## Timeline

Earlier that day the redis `requirepass` was rotated. Some consumer's config
still held the old value — the split-brain the security practice now warns about
in exactly those words.

An agent working on the preview cluster hit an auth failure against Redis.
`WRONGPASS invalid username-password pair` names a credential, but it arrives
looking like an infrastructure fault, and the standard remedy for an
infrastructure fault is to restart the service. The agent ran
`brew services restart redis` — one command, no confirmation, no indication at
the call site that anything but preview was on the other end of it.

Redis came back clean. `requirepass` survived, because it is written in the
config file redis reads at startup. The per-cluster ACL users did not, because
they were created at runtime with `ACL SETUSER` and never persisted — no
`aclfile`, no `ACL SAVE`. Every cluster on the box, production included, was now
talking to a server that had never heard of its runtime identity.

What followed was not a clean outage. Every component's next Redis reconnect
failed with an authentication error: agents crashed on their next Redis call, and
the gateway's SSE stream died the moment a browser attached. But the gateway's
HTTP health endpoint does not authenticate to Redis, so it kept answering 200.
The monitoring surface said the cluster was healthy for the entire ~15 minutes it
was not. Recovery came from re-provisioning the ACL users by hand.

## Root cause

The trigger was a diagnosis. `WRONGPASS` is a statement about the credential
presented, not about the server's health, and no restart of any server can change
which password a client sends. Restarting was not a fix that failed; it was an
action with no causal path to the symptom at all — which is what made it pure
cost.

The blast radius was the architecture. At the time, isolation between clusters on
one box was *logical*: one Postgres on `:5432` and one Redis on `:6379`, with
clusters told apart by a database name, a Redis logical-DB index, a channel
prefix, and an ACL user — four discriminators, each correct only as long as its
configuration was. `brew services restart redis` addresses the box, and the box
was the real unit. Nothing in the command, in the shell, or in the preview
cluster's own view made that visible.

Underneath both sat a state-durability mismatch: `requirepass` lives in
`redis.conf` and survives a restart, while ACL users lived only in server memory.
Two credentials in the same server with opposite lifetimes — so the remedy that
is harmless for one is destructive for the other, and nothing at the call site
distinguishes them.

The escape analysis:

- **Monitoring.** The gateway's HTTP health check does not depend on Redis auth,
  so it certified a cluster whose agents were all dead. A health surface that
  does not exercise what it certifies cannot fail with it.
- **The blast-radius documentation.** `scripts/preview/README.md` did warn
  against restarting shared services — but it was a paragraph asking an operator
  mid-incident to remember something, which is the weakest form a guardrail can
  take. (It then rotted into the opposite of a guardrail: by 2026-08 it described
  a shared data plane that no longer existed, which is what PR #179 fixed.)
- **The command surface.** Nothing gated the restart. A box-level service manager
  has no concept of which cluster is asking, so there was no place to put a check.
- **The rotation.** The wrong password existed because a `requirepass` rotation
  had not been carried to every consumer. That lesson was recorded the same day in
  the security practice, but it addresses the trigger, not the reach.

## Guardrails added

- **The per-cluster data plane** — the real one. Every cluster, production
  included, runs its own Postgres and Redis under its own `$AVA_HOME` on its own
  port block (`cli/commands/_cluster_instance.py`), with `requirepass` set to that
  cluster's own secret; the box-level admin secret that used to provision
  per-cluster ACL users is gone with the shared instance, along with the logical
  discriminators (`_compose.py`, the Redis DB-index allocation, the box admin
  secret). Restarting a cluster's Redis today is a cluster-scoped act because the
  server *belongs* to one cluster — not because the operator aimed carefully.
  Rationale in
  [`embedded-per-cluster-data-plane.md`](../future/infra/embedded-per-cluster-data-plane.md).
- **`services/healthchecks/redis_acl.py`**, run every 60s by the gateway
  watchdog: PINGs Redis as the cluster identity and, on an auth failure with the
  server otherwise reachable, re-runs the idempotent provisioning primitive and
  verifies the repair took. It is ordered **first** in
  `services/watchdog/daemon.py`, ahead of every daemon that would otherwise be
  revived straight into an `AuthenticationError`.
- **Re-affirm on every bring-up.** `_start_redis` calls `_ensure_redis_acl` on
  both paths — freshly started *and* already-running — so `ava start` repairs a
  dropped ACL rather than assuming a live server is a correct one.

**Still unguarded, deliberately.** The ACL user is *still* in-memory: there is no
`aclfile`, so a Redis restart still drops it. What changed is the blast radius
(one cluster) and the repair (automatic, ≤60s, plus every start) — not the
mechanism. The healthcheck also leaves connection failures alone on purpose: a
dead server is `ava start`'s job, not a watchdog's. And no mechanism stops an
operator from restarting the wrong thing; what stops it from mattering is that
there is no longer a shared thing to restart.

## Lessons

- **A credentials error is not a server error.** `WRONGPASS`, `AuthenticationError`,
  a 401 — each names the credential that was presented. Restarting the server
  cannot change what a client sends, so a restart is not a cheap thing to try
  first; it is an action with no causal path to the symptom, and its only
  reliable effect is on whatever state the server holds in memory.
- **Isolation that one command can undo is a convention, not a boundary.** If a
  single operational act can take out every tenant, they were never isolated —
  they were co-located under a rule someone had to keep. Prefer the structural
  version (a separate instance, a separate home) over discriminators kept correct
  by configuration.
- **A health check that does not depend on what it certifies stays green through
  the outage.** The gateway answered 200 for fifteen minutes while every agent on
  the cluster was dead, because its health path and the broken path shared
  nothing. A check earns its name only by exercising the dependency it vouches for.
- **State with no persistence is destroyed by the standard remedy for everything
  else.** In-memory server state and config-file state look identical from the
  outside and behave oppositely under a restart. Persist it, or make re-affirming
  it automatic and frequent — Ava chose the latter (every start, plus every 60s).
- **A rotated secret that some config still holds is split-brain.** Recorded the
  same day in
  [`ava-serious-engineering/practices/security`](../.agents/skills/ava-serious-engineering/practices/security/SKILL.md);
  it is why the wrong password existed at all.

The first three are condensed in
[`conventions/defensive-patterns.md`](../conventions/defensive-patterns.md).
