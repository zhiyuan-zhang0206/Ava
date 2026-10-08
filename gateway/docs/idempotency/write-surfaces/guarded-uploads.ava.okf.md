---
type: doc
title: Guarded delivered upload retry surface
description: Durable source acceptance, native immutable copy and retained inbound outcome.
tags: [gateway, idempotency]
---

# Guarded delivered upload retry surface

| Route | Retry policy | Evidence |
| --- | --- | --- |
| `POST /api/agents/{agent_id}/uploads` | one-shot | optional keyed silent receipt only; delivered legacy copy/chat remain synchronous and cannot safely retry ambiguity |
| `POST /api/keyed/v1/agents/{agent_id}/uploads` | keyed / transactional | required verified principal-v1/key; immutable source acceptance, versioned create-only copy and one retained native inbound outcome |

Browser file delivery submits one batch/key to the new route and validates its
202 source receipt. There is no client Outbox, automatic resend or legacy fallback;
silent native-image uploads and SDK/MCP consumers keep their existing paths.
Old consumers cannot effect the new path/kind. 202 reports historical source acceptance, not remote readiness or
agent execution. Retained status distinguishes pending retries from inspectable
HOLD; there is no time-based reservation expiration or operator cancel endpoint.

Current owner: [delivered uploads](../../../upload_delivery/docs/delivered-uploads/delivered-uploads.ava.okf.md).
Silent/native-image compatibility: [upload batches](../../../routers/docs/upload-batches.ava.okf.md).
