---
type: doc
title: Guarded lifecycle write surfaces
description: Required keys and observed targets for native lifecycle commands.
tags: [gateway, idempotency]
---

# Guarded lifecycle writes

These routes require principal-v1 credential scope and a caller key. Retired
unversioned cancel, compact and retry-launch ingress has no fallback.

| Method and route | Retry policy | Effect / evidence |
|---|---|---|
| `POST /api/keyed/v1/agents/{agent_id}/retry-launch` | keyed | required principal-scoped key and observed prior attempt fix one new launch identity |
| `POST /api/keyed/v1/agents/{agent_id}/cancel-work` | keyed | required principal-scoped key and exact observed native work target |
| `POST /api/keyed/v1/agents/{agent_id}/compact-history` | keyed | required principal-scoped key and exact observed closed history target |
| `POST /api/keyed/v1/agents/{agent_id}/compact-history` | keyed | [[base/agents/compaction/docs/manual-compact/manual-compact.ava.okf.md|guarded manual compact owner]]; no automatic retry |

Acceptance is historical command identity; native status establishes application.
Same-intent recovery retains the original key and target. Current consumers:
[[gateway/agents/docs/control-acceptance.ava.okf.md]] and
[[gateway/agents/docs/launch-retry.ava.okf.md]].
