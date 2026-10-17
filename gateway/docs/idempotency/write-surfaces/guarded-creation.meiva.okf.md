---
type: doc
title: Guarded creation retry surfaces
description: Versioned principal-scoped agent, launch and task acceptance routes
---

# Guarded creation retry surfaces

These fixed versioned paths require a valid caller `Idempotency-Key`,
`Idempotency-Scope: principal-v1` and a verified principal. Same scoped key with
changed semantic request conflicts; different verified principals have
independent namespaces.

| Method and route | Retry policy | Transactional acceptance owner |
|---|---|---|
| `POST /api/keyed/v1/{guide,schedules,packages}/draft` | keyed | raw draft intent and immutable original birth/prompt/attempt |
| `POST /api/keyed/v1/agents` | keyed | original agent birth and first prompt |
| `POST /api/keyed/v1/agents/{agent_id}/retry-launch` | keyed | observed attempt, original replacement attempt and pointer |
| `POST /api/keyed/v1/task-assignments` | keyed | original agent/task pair, assignment inbound and audit facts |

These routes use domain receipts, so the SDK retries an ambiguous failure under
the same key and the retry recovers the original acceptance. Acceptance is not
evidence of runner readiness or completed execution. Launch recovery and
current observations remain in their native owners, outside the acceptance
transaction. None of these routes makes an entire execute_code script atomic.

Current owners:
[[gateway/agents/docs/guarded-drafts.meiva.okf.md]],
[[gateway/agents/docs/guarded-creation.meiva.okf.md]],
[[gateway/agents/docs/launch-retry.meiva.okf.md]] and
[[gateway/agents/task_assignment/docs/task-assignment.meiva.okf.md]].
