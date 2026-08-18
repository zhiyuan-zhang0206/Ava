---
type: doc
title: ava-schedule-writer skill — NL → Gateway-Managed Scheduled Task
description: Transform a user's natural-language scheduling request into a persistent gateway-supervised session — clarify the trigger conditions, write a recoverable Python script, create it via /api/schedules. The script is arbitrary Python (no schedule DSL); the gateway runs it in a PTY-supervisor session, restarts with a circuit breaker on failure.
tags:
- extensions
- agent-instruction
---

# ava-schedule-writer skill — NL → Gateway-Managed Scheduled Task

## What is it
A **schedule** is a persistent session supervised by the gateway: the gateway runs a script in a session, and on crash, restarts with a circuit breaker. This skill (`$AVA_HOME/skills/ava-schedule-writer/`) turns the user's scheduling request into a schedule — clarify what they want, write the script, create it via the API. The key tradeoff it embodies: the script is **arbitrary Python**; the `if`/`for`/threshold logic needed for triggering is written directly in code — **there is no schedule DSL** — placing the burden of "express everything" on code rather than inventing a config language.

## Clarify Three Things (Ask Before Writing)
① **Trigger** — time (wall-clock cadence) / event threshold (conditions on cluster state) / both (fire on either, common shape for consolidation, ava-self-evolution); ② **skip** — which firings should be skipped (no new content / skip weekends); ③ **on error** — on failure, surface (let it raise, runner records in `last_error`) / retry (catch + print to log + try again next round) / ignore (catch and continue).

## Key Dependencies
- [[ava_builtins/skills/ops_lifecycle/ops_lifecycle.ava.okf.md|Ops, Scheduling & Lifecycle Skills]] — parent functional group
- [[gateway/routers/scheduler.ava.okf.md|Scheduler router]] — `/api/schedules` + session supervision + circuit breaker itself
- [[ava_builtins/skills/ops_lifecycle/ava-watcher.ava.okf.md|watcher skill]] — lightweight agent-self background wake-up (contrast: schedule is gateway-managed, survives agent restart)
