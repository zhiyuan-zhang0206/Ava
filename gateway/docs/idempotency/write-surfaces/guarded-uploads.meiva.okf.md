---
type: doc
title: Guarded delivered upload retry surface
description: Durable source acceptance, native immutable copy and retained inbound outcome.
tags: [gateway, idempotency]
---

# Guarded delivered upload retry surface

| Route | Retry policy | Evidence |
| --- | --- | --- |
| `POST /api/agents/{agent_id}/uploads` | keyed / receipt | required verified principal-v1/key; silent save only, claimed then published as immutable objects, replay returns the original receipt |
| `POST /api/keyed/v1/agents/{agent_id}/uploads` | keyed / transactional | required verified principal-v1/key; immutable source acceptance, versioned create-only copy and one retained native inbound outcome |

Browser file delivery submits one batch/key to the new route and validates its
202 source receipt. There is no client Outbox or automatic resend;
silent native-image uploads use the silent route above and SDK/MCP consumers are not activated.
Old consumers cannot effect the new path/kind. 202 reports historical source acceptance, not remote readiness or
agent execution. Copy is attempted at most three times; retained status distinguishes
pending retries from inspectable HOLD, and a HOLD is not retried. There is no
time-based reservation expiration or operator cancel endpoint.

Current owner: [delivered uploads](../../../upload_delivery/docs/delivered-uploads/delivered-uploads.meiva.okf.md).
Silent native-image saves: [upload batches](../../../routers/docs/upload-batches.meiva.okf.md).
