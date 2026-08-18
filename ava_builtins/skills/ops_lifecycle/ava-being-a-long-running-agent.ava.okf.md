---
type: doc
title: ava-being-a-long-running-agent skill — Long-Running Process Discipline
description: Behavioral disciplines for operating as a long-running process — complete tasks rather than just reply, immediately report when stuck, use watchers instead of looping, persist state before compaction, maintain a stable identity. Loaded when responsible for a long-running task or persistent domain; not used for one-off work.
tags:
- extensions
- agent-instruction
---

# ava-being-a-long-running-agent skill — Long-Running Process Discipline

## What is it
A set of **behavioral disciplines** that keep you effective beyond the first few turns (`$AVA_HOME/skills/ava-being-a-long-running-agent/`). Load when you're responsible for a long task or an ongoing domain (watching services, monitoring queues, coordinating peers, driving multi-step pipelines); don't use it for one-off work. It exists because long-running agents have a class of recurring failure modes (treating "replying" as "done", getting stuck silently, using tight loops to wait for events, losing state on compaction); this skill nails those countermeasures as disciplines.

## Six Disciplines
- **End yourself, or stay for a known reason**: before arming watchers and pausing heartbeats, decide whether anything is left to wait for. Task done and no known event pending → terminate yourself (state preserved, a message resurrects you); unsure whether more follows → terminate anyway, resurrection is cheaper than standing by. Stay alive only while a known event is pending or while owning a long-lived role whose work keeps arriving.
- **Complete, don't just reply**: "task done" means you delivered an artifact / wrote a file and gave the path / posted a notice / explicitly handed off. Before each idle period ask "has the world actually changed since my last turn?" — if not, there's probably still work.
- **Immediately report when stuck**: a stuck agent looks identical to a busy agent from the outside; the only difference is whether you make a sound.
- **Use watchers, never loop**: to wait for an external event, arm a watcher and then idle; pair with `ava.self.pause_heartbeat(duration)` to suppress idle check-ins, and use exponential backoff for long waits (each heartbeat wake-up burns a turn + token budget). Neither is a substitute for terminating when the task is done.
- **Persist before compaction**, **maintain a stable identity**.

## Key Dependencies
- [[ava_builtins/skills/ops_lifecycle/ops_lifecycle.ava.okf.md|Ops, Scheduling & Lifecycle Skills]] — parent functional group
- [[ava_builtins/skills/ops_lifecycle/ava-watcher.ava.okf.md|watcher skill]] — the "use watchers" primitive
- [[ava/self.ava.okf.md|ava.self]] — `pause_heartbeat` / `compact` / identity (AGENT_ID)
