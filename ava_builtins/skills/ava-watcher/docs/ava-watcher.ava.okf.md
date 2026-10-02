---
type: doc
title: ava-watcher skill — Background wake-up; don't poll inside your turn
description: Launch a small background program that sends you a message and wakes you when a condition is met (custom condition / specific time / cron) — you write the condition, the watcher waits for you, no turns burned polling. Timeout is mandatory (bounded lifetime); sends an exit notification when it stops on its own or is TTL-reclaimed — not when force-stopped, on Windows, or on a crashed host.
tags:
- extensions
- agent-instruction
---

# ava-watcher skill — Background wake-up; don't poll inside your turn

## What is it
A watcher is a small Python program that runs in the background, independent of your turn, and sends you a message (`ava.agents.send_message(ava.self.AGENT_ID, ...)`) waking you when a condition is met. This skill (`$AVA_HOME/skills/ava-watcher/`) teaches how to use it. Its reason for existing is to eliminate **busy-waiting / polling inside a turn** — you write the condition, the watcher waits for you, saving the turns you'd otherwise burn looping. It's the core primitive for waiting on external events (paired with `pause_heartbeat` to suppress idle check-ins).

## Three Triggers
- **Custom condition** — anything you can check in Python (file landing, build finishing, metric crossing a threshold): write a loop, message yourself when hit, `ava.watcher.launch(code, timeout, name=...)`. **Start the watcher first, then start the thing being waited on**, to avoid missing it.
- **Specific time** — `ava.watcher.at(when, message, name=...)` one-shot wake-up.
- **cron** — `ava.watcher.cron(expr, message, name=...)` periodic wake-up until end_time.

`timeout` **is mandatory**: watchers are always bounded; even if you forget one, it won't run forever. **A watcher that stops on its own sends an exit notification** (exit code + full output pointer + output tail); the platform's own reclaim paths do too for a TTL deadline or a normal `ava stop`/update, but not for `ava stop --force`, a Windows unit, or a crashed host (see [[ava/docs/watcher.ava.okf.md|ava.watcher]]).

## Key Dependencies
- [[ava_builtins/skills/docs/skills.ava.okf.md|Skills index]] — full skills catalog
- [[ava/docs/watcher.ava.okf.md|ava.watcher]] — `at` / `cron` / `launch` SDK itself
- [[ava_builtins/skills/ava-dynamic-workflow/docs/ava-dynamic-workflow.ava.okf.md|ava-dynamic-workflow]] — a **checkpoint** is a custom watcher: workers end silently, one watcher wakes the orchestrator for a whole batch
- [[ava_builtins/skills/ava-being-a-long-running-agent/docs/ava-being-a-long-running-agent.ava.okf.md|being_a_long_running_agent]] — "use watchers, never loop" is one of the long-running disciplines
