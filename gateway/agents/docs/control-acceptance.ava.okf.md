---
type: doc
title: Cancel and Compact Acceptance
description: Observed targets and required principal-scoped keys fence native cancel and manual compaction.
tags: [gateway, agents, idempotency]
---

# Cancel and compact acceptance

Public callers first observe the exact target through
`GET /api/keyed/v1/agents/{agent_id}/native-work` or `compact-target`, then submit
that unchanged target to `POST /api/keyed/v1/agents/{agent_id}/cancel-work` or
`compact-history`. Both writes require `Idempotency-Key`,
`Idempotency-Scope: principal-v1` and a verified credential principal. Missing
identity or unsupported ingress fails before effects. The former `/api/cancel`
and `/api/agents/{agent_id}/compact` routes are removed.

One user action owns one key and one observation. Recovery replays that exact
pair; it never silently observes newer work or history. Changed intent with the
same key conflicts. A deliberate new action gets a fresh observation and key.
Acceptance returns the original `command_id + target`, not proof of application.
Native command status owns the eventual outcome.

The browser captures the observation promise and key before its compaction
mutation can retry. Its stop action observes native work before submitting
cancel. CLI cancel and compact validate both the observation and acceptance
against the requested agent, and print only acceptance. There is no fallback to
retired ingress or implicit resurrection for manual compaction.

Canonical owners describe native admission, checkpoint recovery and settlement:
[[base/agents/incarnation/docs/native-work-cancel.ava.okf.md]] and
[[base/agents/compaction/docs/manual-compact/manual-compact.ava.okf.md]]. The native
compact-envelope insertion primitive remains for graph/history producers and
fixtures; it is not public manual-compaction admission. Historical
`agent_control_receipts` data has no remaining writer; merged migrations and
stored evidence are retained.
