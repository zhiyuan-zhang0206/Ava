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
- `attach(path, label=None)` — Register a local media file for the next turn. The file is read when the current turn ends and reaches the configured model natively. Gated by the model's media capability (ruling 2026-08-28): a text-only model has no attach contract at all (SDK docs omit the member, the call raises), and a file whose modality the model's attach set excludes is rejected at registration with the allowed set — never a silent text-note delivery. Limits are 20 MiB per file, 8 files, and 48 MiB per turn.
- `pause_heartbeat(duration: float)` — Pause idle heartbeat for `duration` seconds (0, 86400]. For waiting on a known external event only (watcher / peer reply / scheduled callback / user answer), never as a substitute for terminating when the work is done. Only suppresses heartbeats, real wake-ups still arrive; a later call replaces the window. Prefer the longest window that fits the wait.
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
