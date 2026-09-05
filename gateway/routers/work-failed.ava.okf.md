---
type: doc
title: Work Failure Feedback Router
description: "POST /api/work-failed durably deduplicates CI, QA, and merge failures, then routes them to the author, a live birth-lineage delegator, or the task registry."
tags:
- gateway
- agents
- delivery
---

# Work failure feedback router

`POST /api/work-failed` accepts `repo`, `ref`, `commit_sha`, `stage`,
`summary`, `author_agent_id`, and a producer-stable `dedup_key`. It uses the
same scoped webhook authentication as alert ingestion: the configured alert
webhook token, the cluster bearer, or tokenless loopback trust. The gateway
records the credential result as inbound provenance; payload fields can never
assert it. Producers obtain `author_agent_id` from the exact
`Co-authored-by: Ava #<id>` commit trailer using `shared/git_identity.py`; the
gateway does not read a Git host.

## Durable routing

The gateway claims `dedup_key` in `work_failed_events` before producing a
delivery effect. A conflict returns the stored event and performs no second
delivery. A new event follows these targets in order:

1. Deliver a `source=system` chat to the author. A terminated author uses the
   existing exact-inbound auto-resurrection path.
2. If the author still lacks a live status and fresh lease, walk immutable
   `born_spawner` edges (with the pre-migration `spawner` fallback) and deliver
   to the nearest live ancestor.
3. If the entire lineage is dead, create a P1 task-registry alert under the
   system root, owned by the author and containing the repository, commit,
   stage, and summary.

The final target is written back as `delivered_to`, `delivery_kind`, and
`delivered_at`. Liveness is always the shared status-plus-lease predicate;
delivery return values do not invent a second liveness truth.

Parent: [[routers.ava.okf.md]].
