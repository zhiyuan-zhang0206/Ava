# Migrating from OpenAI Codex CLI

Codex CLI is OpenAI's open-source coding-agent CLI: an interactive terminal UI,
one-shot `codex exec` runs, cloud tasks, and a GitHub Action. It reads
`AGENTS.md` for project context and is configured through `config.toml`.

> Snapshot as of August 2026, from the official docs. Codex moves fast — treat
> this page as a map, not a contract; confirm flags with `codex exec --help`
> and `codex --version` on the machine.

## Concept map

| Codex CLI | Ava | Notes |
|---|---|---|
| `codex` interactive TUI | Agent dialog (`ava.agents.spawn`) | |
| `codex exec "<task>"` | `ava.shell.run` / `run_background` for command one-shots; spawn an agent for multi-step reasoning tasks | |
| `codex resume` | Hand off via files + the task registry; spawn a fresh agent with the handoff file's path | No conversation resume — the file is the memory. |
| `AGENTS.md` (also reads `.cursorrules`, `CLAUDE.md`) | Same file — Ava surfaces AGENTS.md / CLAUDE.md along resolved paths | |
| `~/.codex/config.toml`, `.codex/config.toml` (`CODEX_HOME`) | `ava config get/set/unset` (cluster + host `.env`) for deployment settings; `ava presets` + `config_overlay` for per-agent model / effort | |
| `approval_policy` (`untrusted` / `on-request` / `never`) | No approval-policy engine. Onboarding records the user's gates as `type/feedback` memory; irreversible actions raise `ava.ui.notify(require_response=True)` | |
| `sandbox_mode` (`read-only` / `workspace-write` / `danger-full-access`) | No sandbox modes. `execute_code` runs in the agent's process and `ava.shell` on the bare host — for untrusted work, delegate to an agent on a disposable machine or a container you stand up | Choose the machine, not the sandbox mode. |
| MCP (`[mcp_servers]` in config.toml) | Same MCP standard; `ava mcp` per machine | |
| Skills (`SKILL.md`) | Same standard — `ava skill install <git-url-or-path>` | |
| Project trust (`[projects."<abs>"] trust_level`) | N/A — agents run under your platform account; no per-project trust prompts | |
| Cloud tasks (`codex --cloud`) | Multi-machine cluster: `ava.agents.spawn(..., machine="<name>")` | |
| GitHub Action | No equivalent shipped — Ava's automation path is gateway schedules + watchers; CI stays CI, agents do the agentic work | |
| `codex login` | Platform auth — no per-agent ChatGPT/API login | |

The bridge works in both directions: `ava mcp serve` exposes the Ava gateway as
an MCP server, so Codex can keep driving the fleet
(`codex mcp add ava -- ava mcp serve`).

## Migration steps

1. **Port skills and MCP servers.** SKILL.md skills install directly
   (`ava skill install`); each `[mcp_servers.<name>]` entry re-registers on
   the machine that has the tools (`ava mcp add`).
2. **Move AGENTS.md as-is** — Ava reads the same file. Project-agnostic rules
   (how to work with you) go to the memory pool as `type/feedback`.
3. **Translate config.toml.** Deployment-level settings → `ava config`;
   per-agent `model` / `model_reasoning_effort` / provider choices → `ava
   presets` (+ `config_overlay` at spawn). There is no `approval_policy` or
   `sandbox_mode` — record the intended behavior as onboarding gates instead.
4. **Rewrite one-shots.** `codex exec` calls become `ava.shell.run` (plain
   commands) or a spawned agent (tasks that need judgment).
5. **Replace session resuming with handoffs.** `codex resume` has no analog;
   instead use tasks in `ava.tasks` and handoff files.

## Differences and pitfalls

- **No approval_policy / sandbox_mode dials.** Ava's answer to dangerous work
  is placement, not sandbox flags: run untrusted or experimental work on a
  dedicated machine or container, and let agents ask before irreversible
  actions. There is no `danger-full-access` to turn off — the agent always
  runs with the process's own permissions on the host.
- **No per-project trust flow.** Codex asks you to trust a project once; Ava
  agents run under your platform account from the start — scope them by
  prompt and workspace instead.
- **The unit of work differs.** Codex is one agent per terminal session; Ava
  is a fleet — one agent per task, tracked in the task registry, with a
  delegator who gathers results.
- **`config.toml` has no single equivalent.** Settings split across `ava
  config` (deployment), presets (agent shape), and memory (behavioral rules).
  When in doubt where a setting goes, ask what it changes: the machine →
  `ava config`, the agent → preset, the behavior → memory.

## Sources

Official Codex docs, accessed 2026-08-12:

- https://developers.openai.com/codex/cli — install, commands, AGENTS.md, sandbox modes, approval policies, MCP, cloud
- https://developers.openai.com/codex/config-reference — config.toml reference
- https://github.com/openai/codex/blob/main/docs/config.md — config keys and locations (repo copy)
