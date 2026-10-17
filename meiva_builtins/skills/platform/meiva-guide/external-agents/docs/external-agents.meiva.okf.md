---
type: doc
title: meiva-guide.external-agents skill — Drive external coding agents
description: Treat claude (Anthropic) / codex (OpenAI) / agy (Google Antigravity) / dsh (DeepSeek Harness) as "another agent", give it tasks, and let it plan+execute. Carries the judgment of when to outsource, to which one, its two modes (a supervised file-driven worker, or a file-less takeover of the launching agent), and the collaboration patterns for both; the session primitives themselves are left to meiva.shell, not repeated here.
tags:
- extensions
- agent-instruction
---

# meiva-guide.external-agents skill — Drive external coding agents

## What it is
`claude`, `codex`, and `agy` (Antigravity) are coding-agent CLIs that can be given tasks and let them plan+execute (`$MEIVA_HOME/skills/meiva-guide/external-agents/`); DeepSeek Harness (`dsh`) joins them as a prompt-driven executor (`scripts/spawn_dsh.py`, relay plugin `scripts/meiva-relay-dsh/`). Treat each as "another agent". `meiva-workflow` owns the delegation decision; this sub-skill owns the **two modes** a session can run in — Mode A, a supervised file-driven worker, and Mode B, a file-less takeover that replaces the launching agent (inline `--prompt`, no task/work files, no supervisor). The session primitives themselves (`meiva.shell`'s run / sessions, `meiva.watcher`) are **not repeated here** — it explicitly points to those, not re-derives.

## Judgments carried
- **Execution surface**: launch scripts run from an Meiva agent context; human and external operators arrange that agent through the CLI. The executor manual comes from the checkout owning the Meiva runtime, independently of where this skill is installed.
- **Tool choice**: verify availability and capability using the actual installed tool; use `meiva-workflow` for task decomposition and verification criteria.
- **Persistent sessions are the default**: start in `meiva.shell.sessions`, steer across rounds; headless one-shot (`claude -p` / `codex exec`) is rarely needed. **Flags and models drift with versions**, text is a snapshot not a contract.
- **Two modes, no leakage**: Mode A's task/work files and supervisor belong to delegated workers only; a takeover runs file-less and supervisor-less (`--prompt`), with start = interruption (checkpoint + pause) and end = one resume system note.

## Canonical Codex lifecycle

The entrypoint retains the mode choice and takeover contract. The file-driven
worker procedure lives in `references/delegated-workers.md`, read after Mode A
is selected; a takeover does not load it. Per-tool and interruption references
remain available independently.

`scripts/spawn_codex.py` is a thin command-line entry over
[[meiva/shell/coding_tools/docs/coding-tools.meiva.okf.md|meiva.shell.coding_tools]] (so are
`scripts/spawn_claude.py` and `scripts/spawn_antigravity.py`). Every launch owns a generation of its own under the
resolved cluster home plus resolved workspace plus `codex`, so several can share
a workspace; the record carries its owner and full Persistent Shell handle, and
the workspace basename appears only in display suffixes. A launch first reclaims
the workspace's dead generations; transitions are serialized by
[[base/sessions/docs/coding-session-owner.meiva.okf.md|the host-local owner journal]].

Codex runs on the host user's own `~/.codex` with per-session `-c` overrides
(workspace trust, no startup update check), so its conversation outlives the
shell: each launch prints the Codex session id (`codex_session`, from
`/status`) and `--resume` reopens it after an interruption (Claude and Antigravity alike, with
`claude_session` and `antigravity_session`); a handoff still starts fresh. The launcher starts a quiet supervisor before Codex only in the
supervised mode (without `--prompt`); it terminalizes the exact generation and reclaims its
PTY/private state on current-generation `DONE` or `HANDOFF`, explicit cancel,
owner termination, Codex death, or absolute expiry. A `--prompt` launch starts no
supervisor — explicit cancel and expiry are its stop paths, and the next
launch reclaims a dead record. Every Codex generation additionally
owns an explicit shared app server, and the launcher wires it: `codex app-server
--listen` on a private per-generation socket (`codex_app_server_socket`), a
janitor that ends the server when the coding session dies, the TUI connected
with `--remote`, and `codex_app_server=<endpoint>` printed so a takeover request
can record it (`--codex-remote`) and the relay delivers into the same server. Notifications
use non-resurrecting system notes.

## Key dependencies
- [[meiva_builtins/skills/docs/skills.meiva.okf.md|Skills index]] — full skills catalog
- [[meiva/shell/docs/shell.meiva.okf.md|meiva.shell]] — `sessions` (new/send/send_keys/capture/kill) session primitive itself
- [[meiva/watcher/docs/watcher.meiva.okf.md|meiva.watcher]] — wait for it to produce results when supervising long tasks
- [[base/sessions/docs/coding-session-owner.meiva.okf.md]] — per-launch generations, the dead-sibling sweep, and exact cleanup
- [[meiva/shell/coding_tools/docs/coding-tools.meiva.okf.md|meiva.shell.coding_tools]] — the launch logic behind the Claude, Codex, and Antigravity spawn scripts
