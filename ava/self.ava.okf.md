---
type: doc
title: ava.self — Agent Self
description: '`ava.self` provides agents with self-control and identity information—who you are, which machine you are on, and how to manage your lifecycle.'
tags:
- agent-view
- sdk
- agent-lifecycle
---

# ava.self — Agent Self

## What it is

`ava.self` provides agents with self-control and identity information—who you are, which machine you are on, and how to manage your lifecycle.

## Core API (core SDK)

### Identity
- `AGENT_ID: int` — Your agent ID (read-only forwarding of an internal framework slot; `AVA_SDK_DISABLE` can remove the self namespace without affecting identity).
- `MACHINE_SPEC: tuple[str, str]` — `(machine_name, description)` current machine name and description.
- `SELF_MACHINE_NAME: str` — Current machine name, also the default value for `ava.agents.spawn(machine=...)`.

### Lifecycle
- `pause_heartbeat(duration: float)` — Pause idle heartbeat for `duration` seconds (0, 86400]. For waiting on a known external event only (watcher / peer reply / scheduled callback / user answer), never as a substitute for terminating when the work is done; docstring guides agent to exponentially back off each time (30 min → 2 hours → longer) to save tokens. Only suppresses heartbeats, real wake-ups still arrive; subsequent calls replace the window.
- `compact(summary) → NoReturn` — Replace entire conversation history with summary, irreversible. First write durable state to MEMORY.md + shared memory pool, then write summary (first person, covering Requests/Progress/In flight/Dead ends/Pitfalls/Verbatim tail).
- `restart(config_overlay=None) → NoReturn` — Replace current process with a new process. `config_overlay` merges into persistent settings.
- `terminate() → NoReturn` — End current process — the normal last step when your task is done (never idle waiting for someone else to do it); state preserved, a message resurrects you.
- ~~`update() → NoReturn`~~ — **removed (2026-08-05, cli-only-updates ruling)**: updates go through `ava cluster update` only; the method is a removed stub (`ava/self.py:update()`).

## ava_fleet Plugin Injections

The following members are mounted onto the `ava.self` namespace by the `ava_fleet` plugin (non-core), allowing agents to report status to the fleet monitoring view. See plugin-side OKF for details.
- `log(text)` — Append a timestamped line to the activity trail; users see the latest behavior/current state in fleet view.
- `set_label(text)` — Set role label (stable role name, not task summary); peers discover you via this label using `get_neighbors`, and your responsibility chain via `get_ancestors`.
- `get_label() → str` — Read current role label (empty string if none).

## Key Dependencies
- [[lifecycle.ava.okf.md]] — underlying implementation of restart/terminate
- [[env-vars.ava.okf.md]] — AGENT_ID environment variable passing

## Notes
`compact()` is irreversible—original conversation history is permanently lost. Must first write critical state to personal memory (workspace `memory/`) or workspace files before calling.
