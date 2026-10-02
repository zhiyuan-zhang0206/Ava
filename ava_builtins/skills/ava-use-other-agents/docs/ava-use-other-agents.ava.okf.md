---
type: doc
title: ava-use-other-agents skill — Drive external coding agents
description: Treat claude (Anthropic) / codex (OpenAI) / dsh (DeepSeek Harness) as "another agent", give it tasks, and let it plan+execute. Carries the judgment of when to outsource, to which one, its two modes (a supervised file-driven worker, or a file-less takeover of the launching agent), and the collaboration patterns for both; the session primitives themselves are left to ava.shell, not repeated here.
tags:
- extensions
- agent-instruction
---

# ava-use-other-agents skill — Drive external coding agents

## What it is
`claude` and `codex` are both coding-agent CLIs that can be given tasks and let them plan+execute (`$AVA_HOME/skills/ava-use-other-agents/`); DeepSeek Harness (`dsh`) joins them as a takeover-only executor (`scripts/spawn_dsh.py`, relay plugin `scripts/ava-relay-dsh/`). Treat each as "another agent". This skill carries **judgment**: when to outsource, to which one, and the **two modes** a session can run in — Mode A, a supervised file-driven worker, and Mode B, a file-less takeover that replaces the launching agent (inline briefing, no task/work files, no supervisor). The session primitives themselves (`ava.shell`'s run / sessions, `ava.watcher`) are **not repeated here** — it explicitly points to those, not re-derives.

## Judgments carried
- **When to outsource**: single file read/write / grep / git / a single command → do it yourself; multi-step coding tasks that can be fully described (write+test+fix), expected >10 files with multiple rounds of trial and error → outsource.
- **Which one**: if you need a specific model's reasoning / long context, choose the tool backed by that model; for diff review, use `codex exec review` or `claude -p`. A machine may only have one installed — verify with `--version`/`--help` beforehand.
- **Persistent sessions are the default**: start in `ava.shell.sessions`, steer across rounds; headless one-shot (`claude -p` / `codex exec`) is rarely needed. **Flags and models drift with versions**, text is a snapshot not a contract.
- **Two modes, no leakage**: Mode A's task/work files and supervisor belong to delegated workers only; a takeover runs file-less and supervisor-less, its briefing inlined, with start = interruption (checkpoint + pause) and end = one resume system note.

## Canonical Codex lifecycle

`scripts/spawn_codex.py` is a thin command-line entry over
[[ava/shell/coding_tools/docs/coding-tools.ava.okf.md|ava.shell.coding_tools]] (so is
`scripts/spawn_claude.py`). Every launch owns a generation of its own under the
resolved cluster home plus resolved workspace plus `codex`, so several can share
a workspace; the record carries its owner and full Persistent Shell handle, and
the workspace basename appears only in display suffixes. A launch first reclaims
the workspace's dead generations; transitions are serialized by
[[base/sessions/docs/coding-session-owner.ava.okf.md|the host-local owner journal]].

Codex runs on the host user's own `~/.codex` with per-session `-c` overrides
(workspace trust, no startup update check), so its conversation outlives the
shell: each launch prints the Codex session id (`codex_session`, from
`/status`) and `--resume` reopens it after an interruption (Claude alike, with
`claude_session`); a handoff still starts fresh. The launcher starts a quiet supervisor before Codex only in the
supervised mode; it terminalizes the exact generation and reclaims its
PTY/private state on current-generation `DONE` or `HANDOFF`, explicit cancel,
owner termination, Codex death, or absolute expiry. A takeover starts no
supervisor — explicit cancel and expiry are its stop paths, and the next
launch reclaims a dead takeover record. A takeover generation additionally
owns an explicit shared app server, and the launcher wires it: `codex app-server
--listen` on a private per-generation socket (`codex_app_server_socket`), a
janitor that ends the server when the coding session dies, the TUI connected
with `--remote`, and the endpoint in the launch message so the request records
it (`--codex-remote`) and the relay delivers into the same server. Notifications
use non-resurrecting system notes.

## Key dependencies
- [[ava_builtins/skills/docs/skills.ava.okf.md|Skills index]] — full skills catalog
- [[ava/shell/docs/shell.ava.okf.md|ava.shell]] — `sessions` (new/send/send_keys/capture/kill) session primitive itself
- [[ava/docs/watcher.ava.okf.md|ava.watcher]] — wait for it to produce results when supervising long tasks
- [[base/sessions/docs/coding-session-owner.ava.okf.md]] — per-launch generations, the dead-sibling sweep, and exact cleanup
- [[ava/shell/coding_tools/docs/coding-tools.ava.okf.md|ava.shell.coding_tools]] — the launch logic behind the Claude and Codex spawn scripts
