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

## Ownership and procedures

The core system prompt's **Efficient long-running operation** section carries
lifecycle and cost principles even without fleet or this skill being loaded.
This skill owns the operational procedures: record awaited events and monitor
references, select event delivery or background mechanical checks, set response
requirements and heartbeat pauses, persist progress, and restore missing monitors
without duplicating survivors. Ordinary metric changes do not require model
wakes; useful wakes carry a trigger, evidence, and a durable record pointer.

The fleet plugin owns peer communication. This skill refers to that contract for
milestones, commitments, blockers, and handoffs rather than prescribing a second
cadence. The human interruption section remains separate.

## Key Dependencies
- [[ava_builtins/skills/docs/skills.ava.okf.md|Skills index]] — full skills catalog
- [[ava/docs/watcher.ava.okf.md|Watcher SDK]] — the "use watchers" primitive
- [[ava/docs/self.ava.okf.md|ava.self]] — `pause_heartbeat` / `compact` / identity (AGENT_ID)

## Usage reminders

Usage budget reminders compare recorded USD cost with `--usd-limit`. Select
explicit agent IDs, a window or lifetime, and immutable spawn/fork lineage.
Token counts are usage observations, never budget thresholds. Reminders notify
named peers once and leave graceful disposition to the receiving agents.
