---
type: doc
title: "Write Retry Surface Inventory"
description: "Audit snapshot of non-GET gateway routes and their verified retry policies."
tags:
- gateway
- idempotency
---

# Write retry surface inventory

Non-GET snapshot; live policy: `base/api_contracts/contracts.py`.
`natural` means repeatable effects/CAS;
`keyed` requires a business receipt; `one-shot` forbids ambiguous automatic retries.
Login intentionally mints sessions; telemetry inserts events; upstream writes
inherit no safe-retry promise. Machine control has a separate operator boundary.

Preset/schedule creation and schedule mutations now have optional transactional
keyed receipts, but their routes conservatively remain `NON_IDEMPOTENT` (`one-shot`
below). Keyless calls retain legacy semantics. Positive server negotiation and
ambiguous retry activation remain future work; older gateways may ignore keys.
Domain evidence:
[[gateway/routers/docs/resource-creation.ava.okf.md]] and
[[gateway/schedules/docs/schedule-convergence.ava.okf.md]].

| Method and route | Retry policy | Effect / evidence |
|---|---|---|
| `PATCH /api/agents/{agent_id}` | natural | label patch — CAS update |
| `POST /api/agents` | keyed | immutable creation identity commits with birth and first prompt; SDK ambiguous retry stays gated for older gateways |
| `POST /api/agents/{agent_id}/retry-launch` | one-shot | explicit same-ID launch retry rotates the attempt key; each call dispatches once |
| `POST /api/alerts` | natural | Grafana Alertmanager webhook — upsert per (fingerprint, starts_at); a 503 exhausts Grafana retries and the alert is lost |
| `POST /api/auth/login` | natural | login — repeats just mint a fresh cookie |
| `POST /api/auth/logout` | natural | clear session cookie — idempotent |
| `POST /api/auth/sessions/{session_id}/revoke` | natural | session revocation — guarded update; repeats cannot revoke twice |
| `POST /api/cluster/stopping` | one-shot | control-plane op — host self-report during stop |
| `DELETE /api/cluster/machines/{name}` | one-shot | control-plane op — deregisters a machine |
| `POST /api/cluster/machines/{name}/staging` | natural | control-plane op — operator sets/clears the staging flag |
| `POST /api/cluster/machines/{name}/pause` | natural | control-plane op — drains tasks, terminates the machine's agents, sets the pause latch (idempotent: re-pause is a safe no-op) |
| `POST /api/cluster/machines/{name}/resume` | natural | control-plane op — clears the pause latch (idempotent no-op when not paused) |
| `PUT /api/config` | natural | full config replace — PUT is idempotent |
| `PUT /api/config/default-model` | natural | set default model — PUT is idempotent |
| `POST /api/event-resolutions` | one-shot | create class dismissal — INSERT; retries receive a conflict rather than a replay |
| `POST /api/event-resolutions/{dismissal_id}/reopen` | natural | guarded active-state transition — repeats cannot reopen twice |
| `POST /api/frontend-telemetry` | one-shot | telemetry ingest — pure INSERT per event; a retry duplicates rows (client never retries) |
| `POST /grafana/{rest:path}` | one-shot | reverse proxy — semantics follow upstream |
| `PATCH /grafana/{rest:path}` | one-shot | reverse proxy — semantics follow upstream |
| `DELETE /grafana/{rest:path}` | one-shot | reverse proxy — semantics follow upstream |
| `PUT /grafana/{rest:path}` | one-shot | reverse proxy — semantics follow upstream |
| `POST /api/guide/draft` | one-shot | LLM generation incurs a fresh external request and token cost |
| `PUT /api/inventory` | natural | full inventory replace — PUT is idempotent |
| `POST /api/agents/{agent_id}/impersonation/force-expire` | natural | observed-session CAS close — repeated or stale requests leave the lease unchanged |
| `POST /api/agents/{agent_id}/compact` | one-shot | each request enqueues a new compact command; no durable command receipt |
| `POST /api/cancel` | one-shot | each request enqueues cancel; a delayed retry can cancel later work |
| `POST /api/agents/{agent_id}/terminate` | one-shot | termination is not bound to the observed incarnation |
| `POST /api/agents/{agent_id}/resurrect` | one-shot | resurrection is not bound to the observed incarnation |
| `POST /api/agents/resurrect-billing` | one-shot | billing resurrection has no durable operation receipt; preview alone is read-only |
| `POST /api/agents/{agent_id}/restart` | one-shot | a repeat dispatches another restart; HTTP intent has no durable receipt |
| `POST /api/agents/{agent_id}/understanding/close` | natural | plans one closing job from stored state; a repeat finds the active job or an empty stretch |
| `POST /api/mcp/clients` | one-shot | client creation — plaintext token is revealed once |
| `POST /api/mcp/clients/{client_id}/revoke` | natural | client revocation — guarded update; repeats cannot revoke twice |
| `POST /api/memory/refresh` | natural | re-scan — repeats are harmless |
| `POST /api/memory/search` | natural | pure read |
| `POST /api/agents/{agent_id}/notices/{notice_id}/resolve` | keyed | operation receipt and reply inbound commit together; intentional later replies use new keys |
| `POST /api/agents/{agent_id}/notices` | keyed | immutable request and original notice snapshot replay before mutable expiry/task checks |
| `PATCH /api/agents/{agent_id}/notices/current` | natural | edit current open notice — repeats are harmless |
| `POST /api/agents/{agent_id}/notices/current/dismiss` | natural | withdraw current open notice — CAS, repeats are harmless |
| `POST /api/packages/draft` | one-shot | LLM generation incurs a fresh external request and token cost |
| `POST /api/agents/{agent_id}/pages` | natural | register page — upsert, repeats are harmless |
| `DELETE /api/agents/{agent_id}/pages/{name}` | natural | close page — CAS, repeats are harmless |
| `POST /api/presets` | one-shot | optional keyed creation receipt commits with the resource; replay returns original identity after rename/delete |
| `PATCH /api/presets/{preset_id}` | natural | update — repeats are harmless |
| `DELETE /api/presets/{preset_id}` | natural | delete — repeats are harmless |
| `POST /api/schedules` | one-shot | optional keyed creation receipt commits with resource/version; replay returns original identity after rename/delete |
| `POST /api/schedules/draft` | one-shot | LLM generation incurs a fresh external request and token cost |
| `POST /api/schedules/{schedule_id}/start` | one-shot | optional keyed receipt commits desired state/sync; replay returns original acceptance without re-enabling later state |
| `POST /api/schedules/{schedule_id}/stop` | one-shot | optional keyed receipt commits desired state/sync; unchanged disabled state is a no-op |
| `POST /api/schedules/{schedule_id}/restart` | one-shot | optional keyed receipt commits one new desired revision/sync; same-key replay does not restart again |
| `PUT /api/schedules/{schedule_id}` | one-shot | optional keyed receipt commits edit/version/revision/sync; same-value edits are no-ops |
| `DELETE /api/schedules/{schedule_id}` | natural | delete — repeats are harmless |
| `PUT /api/settings/{key}` | natural | set one key — PUT is idempotent |
| `PUT /api/skills` | natural | full replace — PUT is idempotent |
| `POST /api/agents/{agent_id}/messages` | keyed | enqueue chat inbound — one logical message must land exactly once; clients retry with an Idempotency-Key |
| `POST /api/agents/{agent_id}/messages/reconcile` | natural | idempotent receipt recovery — heals the pending wake/resurrection tail for an uncertain same-key delivery |
| `POST /api/agents/{agent_id}/system-note` | keyed | optional principal-scoped identity reuses one system-note inbound; changed payload or resurrection policy conflicts |
| `PATCH /api/tasks/{task_id}` | one-shot | task effect and notification share a transaction; request has no replay receipt |
| `POST /api/agents/{agent_id}/uploads` | one-shot | [upload acceptance owner](../../routers/docs/upload-batches.ava.okf.md) |
