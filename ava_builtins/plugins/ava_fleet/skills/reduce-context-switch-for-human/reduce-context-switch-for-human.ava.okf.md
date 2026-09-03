---
type: doc
title: reduce-context-switch-for-human — Interrupt Discipline
description: Interrupt discipline when human attention is elsewhere (added in #620): default to queueing via ava.ui.notify, never push; one rolling notice per manager, updates are milestone-based by default (user ruling 2026-09-03); progress rolls up along the delegation tree; out-of-band push is only allowed for truly urgent matters.
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
- **Updates are milestone-based by default** (formerly "Batch boundary" →
  "agreed at delegation time", now fixed by user ruling 2026-09-03): one
  notice per manager — a single rolling update, a new `ava.ui.notify`
  replaces the old one in place, the queue will not accumulate a manager's
  history. Roll up on a **real milestone, a blocker, completion, or a real
  need**; routine progress and bare acknowledgments are noise — each
  message costs the recipient a turn. A different interval is the
  exception: a delegator that wants one names it explicitly in the brief.
  **Budget remains per-delegation** — budget cap and any reporting
  exception are both part of the brief (for soft enforcement of budget see
  [[ava_builtins/plugins/ava_fleet/skills/ava-fleet/ava-fleet.ava.okf.md|ava-fleet skill]]'s Autonomy boundaries).
- **Direct reply means user has responded**: when the user sends you a direct message, judge whether this exchange resolves the open notice you have — if resolved, withdraw it (or let a new notify replace it); if not, keep or resend. Whether to clear is judged by the agent itself, not automatically closed by the framework.
- **Roll-up dichotomy**: authorization/decisions go directly to the user from any depth; progress/conclusions roll up to the manager (owner of the parent task) for aggregation; when there is no task and no manager, deliver directly (do not fabricate hierarchy).

## Push Posture: Only Push for Truly Urgent Matters

Out-of-band pushes are only allowed for truly urgent matters: irreversible risk en route / entirely blocked on user / user explicitly asked to be woken. Everything else stays in the queue. This is a hardcoded discipline in the skill text, **not enforced by the framework**. push = deliver to where the human actually is, channel chosen by agent — the skill determines *when*, not *how*.

## Key Dependencies

- [[ava_builtins/plugins/ava_fleet/notify.ava.okf.md|Notify]] — queue channel itself (`ava.ui.notify`)
- [[ava_builtins/plugins/ava_fleet/skills/ava-fleet/ava-fleet.ava.okf.md|ava-fleet skill]] — fleet general outline of the same communication dichotomy
- [[ava_builtins/skills/comms/comms.ava.okf.md|Communication skills group]] — push channel skills like telegram
