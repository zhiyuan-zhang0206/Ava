---
type: doc
title: Anti-RL-Bias Mechanisms
description: How Ava answers two RLHF habits that hurt a long-lived agent (declaring done after a text turn, going quiet when unsure) with verbs, daemons and hooks instead of prompt pleading.
tags:
- agent
- plugins
- heartbeat
---

# Anti-RL-Bias Mechanisms

RLHF-tuned models carry two habits that hurt a long-lived agent: treating a
text turn as "I answered, therefore I'm done", and going quiet when unsure. Ava
answers both from the system layer — first-class verbs, daemons and hooks that
turn ambiguous silence into supervisable state. A fleet that runs unattended
cannot afford agents that look done when they are not, or that stall silently
instead of asking; an agent that declares intent beats a prompt that asks it to.

## Mechanisms

1. **Idle heartbeat** — `services/wake/heartbeat/` ([[services/docs/gateway_side/heartbeat.ava.okf.md]]).
   A gateway daemon polls idle agents and sends a nudge naming three honest
   options: still working (do nothing), waiting (`ava.self.pause_heartbeat(duration)`
   suppresses nudges for a declared window; real wake-ups still arrive), or done
   (terminate). Opt-out is an active agent choice, not an escalation chain
   ([why](../../docs/decisions/runtime/processes/health/2026-06-22-heartbeat-opt-out-over-escalation.md)).
2. **Silent-idle continue nudge** — `ava_builtins/plugins/ava_silent_idle/`
   ([[ava_builtins/plugins/ava_silent_idle/docs/ava_silent_idle.ava.okf.md]]).
   When the model produces reasoning but no text and no tool call, the kernel
   keeps the reasoning in context and loops back, and a hook injects a nudge to
   produce text or a tool call, or to state completion in text. A consecutive-count
   guard bounds a model that habitually stalls.
3. **SDK reminder** — `ava_builtins/plugins/ava_sdk_reminder/`
   ([[ava_builtins/plugins/ava_sdk_reminder/docs/ava_sdk_reminder.ava.okf.md]]).
   Hints the first time the agent reaches for a native-Python equivalent
   (`subprocess` → `ava.shell.run`, `time.sleep` loops → `ava.watcher`,
   `open()` → `ava.files`, `requests` → `ava.web`), and reminds an agent that
   a plain text reply reaches nobody — the verb is `ava.agents.send_message`.
4. **Capabilities drift check** — `agent/hooks/capabilities.py`. Before every LLM
   call the live skill index is diffed against the snapshot the context window
   was built from; skills installed since are named in one system note, so an
   agent cannot silently rebuild a capability it already has.
5. **Task progress reminders** — the gateway tasks API (`remind_interval_seconds`).
   The owner of an in-progress task is periodically reminded to post progress;
   reminders cannot be disabled (capped at 24 h), so a task that goes dark is a
   visible fact.
6. **Delivery semantics** — a bare text turn delivers nothing:
   `ava.agents.send_message` (peers) and `ava.ui.notify` (the user) are the only
   delivery verbs, so "not done yet" must be said out loud and silence reads as
   busy ([why](../../docs/decisions/agents/messages/2026-08-01-one-send-is-one-inbound.md)).
7. **Standing memory injection** — the memory index (`MEMORY.md`) is injected at
   cold start and after every compact, so long-lived rules stay in front of the
   agent ([[agent/graph/docs/context-notes/memory-injection.ava.okf.md]]).
8. **Prompt-layer discipline** — agents give scope and trade-offs, never time
   estimates, and describe current behavior
   ([`communicating-with-user.md`](../../docs/conventions/communicating-with-user.md)).
