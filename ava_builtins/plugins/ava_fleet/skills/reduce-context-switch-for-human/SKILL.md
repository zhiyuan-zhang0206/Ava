---
name: reduce-context-switch-for-human
description: Rolls delegated progress up the manager tree and reserves user interruptions for real emergencies. Use when the human's attention is elsewhere, when choosing queue versus push, or when pinning a delegation's reporting discipline.
---

# Reduce Context Switch for the Human

Every push costs the human a context switch. When their attention is elsewhere, the default is silence: work lands in queues they drain on their own schedule.

## Default: queue, never push

The human-facing channel is `ava.ui.notify` — a queue the user reads when they choose. It carries only what survives the roll-up below: a manager's aggregated results, or deliveries from an agent with no manager; a delegated agent's progress goes to its manager via `send_message`, not to the queue. Delivering to the queue IS delivering; do not escalate just because nothing was acknowledged.

## Cadence is a delegation contract

One notice per manager — its single rolled-up view of the subtree, updated in place: a new `ava.ui.notify` supersedes the old one, so the queue never accumulates a manager's history.

Updates are **milestone-based by default** (user ruling 2026-09-03): roll
up on a real milestone, a blocker, completion, or a real need — never
routine progress, never a bare acknowledgment. A delegator that wants a
different pattern names it in the brief; that is the exception, not a
per-delegation negotiation. The brief still carries the goal, boundaries,
budget, and any reporting exception.

## A direct reply is the user responding

When the user messages you directly, they have responded to you — judge whether that exchange resolves the notice you left open. If it does, withdraw it (or let a new `ava.ui.notify` supersede it); if their message did not answer what you were waiting on, keep it or re-post. Nothing clears the notice for you — it is your judgment, not a framework action.

## Roll-up bisection

- **Authorization / decision** — something only the user may decide — goes to the user directly, from any depth in the tree.
- **Progress / conclusions** go to your manager (the parent task's owner), who digests and aggregates before anything reaches the user's queue.
- No task, no manager: deliver directly; do not manufacture a hierarchy to relay through.

## Emergency push

Push only for a true emergency: irreversible risk in motion, the whole effort blocked on the user, or the user explicitly asked to be woken for this. Everything else queues.

A push is out-of-band delivery to wherever the human actually is; find the channel yourself (e.g. a push skill like telegram). This skill sets when, not how.
