---
type: doc
title: Notify — User Notification System
description: "meiva.ui.notify namespace: push notifications (FYI / response required / blocking) to user queues, edit pending notifications, dismiss expired notifications. This is the only reliable channel for agents to report results and request decisions from humans."
tags:
- fleet
- notify
- user-communication
---

# Notify — User Notification System

## Responsibility

The notification system is the only reliable channel for agents to push messages to users. The user's aggregated notification feed shows only content sent via
`meiva.ui.notify`—the agent's ordinary text output is not included.

## Three Notification Tiers

| Tier | Parameters | Purpose | Agent Behavior |
|------|------------|---------|----------------|
| **FYI** | `require_response=False` | Milestones, minor issues already bypassed, directional choices | Continues working, does not wait for a reply |
| **Response Required** | `require_response=True` | Decisions only a human can make (irreversible operations, spending money, sending messages to real people) | Can continue doing other things |
| **Blocking** | `require_response=True, blocking=True` | Same as above + agent cannot proceed until a reply | Ends turn after posting notification, idles waiting |

**Communication bisection** (roll-up, #620): **authorization / decisions** (irreversible, spending money, messaging real people) reach the user directly from any depth via `notify`; **progress / conclusions** go to one's delegator via `send_message` for aggregation—the user queue holds only manager-level rollups or deliveries from delegator-less agents; peer-decidable things go to the peer. Default to queueing, never push—out-of-band push only when truly urgent, see [[meiva_builtins/plugins/meiva_fleet/skills/reduce-context-switch-for-human/docs/reduce-context-switch-for-human.meiva.okf.md|interruption discipline skill]].

## API

### `meiva.ui.notify(title, content=None, *, require_response=False, blocking=False, priority='P2', task=None, expire_at=None, idempotency_key, question=None) -> Notice`

Push a notification, automatically superseding any previously unresolved notification from the same agent (resolution=`"superseded"`).

- `idempotency_key` (required): retain this operation key for the same notice across retries; use an absolute expiration deadline. Missing or invalid keys fail before HTTP.
- `title` (required): A one-line title; this is exactly what the user sees in their queue.
- `content` (optional): Detailed content displayed when the user opens the notification. For a structured question, use this as background and put the question itself in `title`; legacy notices may still offer A/B/C in plain text.
- `question` (optional): one `text`, `single_select`, or `multi_select` question. Choice questions carry ordered `options` (`id`, `label`, optional `description`) and `allow_other`. Requires `require_response=True`.
- `priority`: `"P0"` (highest) to `"P3"` (lowest), suggesting how the user should sort.
- `task` (optional, #663): the owning task's ID—the human queue groups by the notice's **own** task, not just the owner agent; validated at post time (`ValueError` if absent, no dangling FK); immutable after posting (`edit_notice` rejects it).
- Returns a `Notice` object—integer id (for reference) + `.pending_count`, `.pending_notices`, `.superseded`.

**Constraints**: title non-empty; priority one of `"P0"`..`"P3"`; `blocking=True` requires `require_response=True`; at most one active notice per agent; the named `task` must exist.

### `meiva.ui.edit_notice(*, title=..., content=..., priority=..., blocking=...) -> None`

Modify the current pending notice. Pass only changed fields. Structured questions freeze `title`, `content`, and the ordered question when posted; replace the notice to change their meaning. Priority and blocking can still be edited. The call observes the open notice id first and edits exactly that row under one key; an ambiguous response is retried under the same key and replays the original acceptance, never editing a newer notice.

- Cannot change `require_response` (to change it, dismiss + re-notify)
- `content=None` clears the content
- `blocking=True` is only valid for notices with `require_response=True`
- No open notice → idempotent no-op

### `meiva.ui.dismiss_notice() -> None`

Withdraw the current open notice (resolution=`"withdrawn"`); no open notice is a no-op. An ambiguous response is surfaced without automatic replay, which could withdraw a newer notice. Inspect current state before an explicit new intent.

### ~~`meiva.ui.list_notices`~~ (removed 2026-08-02)

At most one notice is open per agent (notify auto-resolves the previous), so the agent-facing list verb had no consumer-side reason to exist — its dominant usage was recovering the id for edit/dismiss, which no longer take ids. The unified inbox (frontend) remains the consumer of the resolved history; agents who need their own notice rows can query the table directly.

## Numbered questions

A notice carries one immutable `text`, `single_select`, or `multi_select` question.
Options keep their declared 1-based order; Other follows them with the final number.
Web submits structured answers and CLI/IM use the same validator for numbered
text replies. See [[notice-questions.meiva.okf.md]] for examples, answer
rules and the text-client grammar. Existing notices remain free-text.

## Data Model

```
agent_notices (table)
├── agent_id          — the agent that sent the notice
├── local_id          — local per-agent incrementing sequence number (UNIQUE(agent_id, local_id))
├── task_id           — the owning task (nullable FK → agent_tasks, added in #663; validated at post time, immutable after posting)
├── title, content    — title and content
├── priority          — P0..P3
├── require_response  — whether a response is required (NOT NULL)
├── blocking          — whether it blocks (CHECK: NOT blocking OR require_response)
├── created_at
├── updated_at        — last edit / status change
├── resolved_at       — NULL means unresolved (CHECK: (resolved_at IS NULL) = (resolution IS NULL))
├── resolution        — 'answered' | 'dismissed' | 'read' | 'withdrawn' | 'superseded'
├── question          — immutable JSONB question; NULL for legacy notices
├── answer            — validated JSONB answer; NULL until answered
└── reply             — cached readable reply (free text for legacy notices, server projection for structured answers); delivery uses system:notice-reply
```

`task_id` propagates to the gateway notice read path (snapshots, `NoticeItem`, `notice_posted` SSE) and the `notice_posted`/`notice_resolved` events—the frontend groups the needs-you queue by the notice's own task, falling back to owner-join grouping without one.

Five resolution values (guarded by the `agent_notices_resolution_legal` CHECK, restricted by `require_response`):
- `answered` — the user answered a require_response notice (reply = answer)
- `dismissed` — the user dismissed a require_response notice without answering
  (delivery: a `system:notice-dismiss` system-sourced inbound — a system event,
  not user speech — so the agent stops waiting without a User-role message)
- `answered` / `read` with a reply deliver `system:notice-reply` — the reply is
  notice-system content, not user speech, so no User-role message is consumed
- `read` — the user viewed an FYI notice (reply optional; only for NOT require_response)
- `withdrawn` — agent withdrew it (`meiva.ui.dismiss_notice`)
- `superseded` — replaced by a new notify from the same agent

## Relationships to Other Subsystems

- [[self.meiva.okf.md]] — notify acts on behalf of the agent for its owner
- [[neighbors.meiva.okf.md]] — judging "can a peer decide or must a human decide" depends on neighbor discovery
- [[meiva_builtins/plugins/meiva_fleet/docs/meiva_fleet.meiva.okf.md]] — overview
