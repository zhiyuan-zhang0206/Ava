---
type: doc
title: reduce-context-switch-for-human — Interrupt Discipline
description: Interrupt discipline when human attention is elsewhere (added in #620): default to queueing via ava.ui.notify, never push; one rolling notice per manager, update cadence agreed upon at delegation time (not a framework-fixed batch boundary); progress rolls up along the delegation tree; out-of-band push is only allowed for truly urgent matters.
tags:
- fleet
- agent-instruction
- user-communication
---

# reduce-context-switch-for-human — Interrupt Discipline

## What it is

Every push costs a human a context switch. This skill (`plugins/ava_fleet/skills/reduce-context-switch-for-human/SKILL.md`) stipulates: when the user's attention is elsewhere, default to **silence** — work lands in a queue that the user retrieves at their own pace.

## Discipline

- **Default queue, never push**: the human-facing channel is `ava.ui.notify` (queue semantics). The queue only holds roll-up survivors — manager's aggregated results, or deliveries from agents without a manager; delegated agents' progress goes via `send_message` to the manager, not into the queue. **Delivery into the queue is completion**, no escalation due to lack of ack.
- **Cadence is an agreement at delegation time** (formerly "Batch boundary", revised): one notice per manager — a single rolling update, a new `ava.ui.notify` replaces the old one in place, the queue will not accumulate a manager's history. How often to update is **agreed upon at delegation time** by the delegator and executor based on the nature of the work — it can be per PR, per day, per milestone, or only once upon completion; different agents' cadences can differ by orders of magnitude. The framework does not fix the interval or trigger condition; **cadence sits alongside budget** — budget cap and cadence are both part of the per-delegation agreement (for soft enforcement of budget see [[ava_builtins/plugins/ava_fleet/skills/ava-fleet/ava-fleet.ava.okf.md|ava-fleet skill]]'s Autonomy boundaries).
- **Direct reply means user has responded**: when the user sends you a direct message, judge whether this exchange resolves the open notice you have — if resolved, withdraw it (or let a new notify replace it); if not, keep or resend. Whether to clear is judged by the agent itself, not automatically closed by the framework.
- **Roll-up dichotomy**: authorization/decisions go directly to the user from any depth; progress/conclusions roll up to the manager (owner of the parent task) for aggregation; when there is no task and no manager, deliver directly (do not fabricate hierarchy).

## Push Posture: Only Push for Truly Urgent Matters

Out-of-band pushes are only allowed for truly urgent matters: irreversible risk en route / entirely blocked on user / user explicitly asked to be woken. Everything else stays in the queue. This is a hardcoded discipline in the skill text, **not enforced by the framework**. push = deliver to where the human actually is, channel chosen by agent — the skill determines *when*, not *how*.

## Key Dependencies

- [[ava_builtins/plugins/ava_fleet/notify.ava.okf.md|Notify]] — queue channel itself (`ava.ui.notify`)
- [[ava_builtins/plugins/ava_fleet/skills/ava-fleet/ava-fleet.ava.okf.md|ava-fleet skill]] — fleet general outline of the same communication dichotomy
- [[ava_builtins/skills/comms/comms.ava.okf.md|Communication skills group]] — push channel skills like telegram
