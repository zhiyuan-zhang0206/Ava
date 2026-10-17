---
type: doc
title: meiva-guide.schedules skill — NL → Gateway-Managed Scheduled Task
description: Transform a user's natural-language scheduling request into a persistent gateway-supervised session — clarify the trigger conditions, write a recoverable Python script, create it via /api/schedules. The script is arbitrary Python (no schedule DSL); the gateway runs it in a PTY-supervisor session, restarts with a circuit breaker on failure.
tags:
- extensions
- agent-instruction
---

# meiva-guide.schedules skill — NL → Gateway-Managed Scheduled Task

## What is it
A **schedule** is a persistent session supervised by the gateway: the gateway runs a script in a session, and on crash, restarts with a circuit breaker. This skill (`$MEIVA_HOME/skills/meiva-guide/schedules/`) turns the user's scheduling request into a schedule — clarify what they want, write the script, create it via the API. The key tradeoff it embodies: the script is **arbitrary Python**; the `if`/`for`/threshold logic needed for triggering is written directly in code — **there is no schedule DSL** — placing the burden of "express everything" on code rather than inventing a config language.

## Clarify Three Things (Ask Before Writing)
① **Trigger** — time (wall-clock cadence) / event threshold (conditions on cluster state) / both (fire on either, common shape for consolidation, meiva-self-evolution); ② **skip** — which firings should be skipped (no new content / skip weekends); ③ **on error** — on failure, surface (let it raise, runner records in `last_error`) / retry (catch + print to log + try again next round) / ignore (catch and continue).

## Key Dependencies
- [[meiva_builtins/skills/docs/skills.meiva.okf.md|Skills index]] — full skills catalog
- [[gateway/schedules/docs/schedules.meiva.okf.md|Scheduler router]] — `/api/schedules` + session supervision + circuit breaker itself
- [[meiva/watcher/docs/watcher.meiva.okf.md|Watcher SDK]] — lightweight agent-self background wake-up (contrast: schedule is gateway-managed, survives agent restart)
