# Migrating from Claude Code

Claude Code is Anthropic's coding-agent CLI: an interactive terminal session per
project, one-shot headless runs (`claude -p`), VS Code / desktop / web surfaces,
and a stack of extension mechanisms — CLAUDE.md memory files, Agent Skills,
subagents, hooks, slash commands, and MCP servers.

> Snapshot as of August 2026, from the official docs. Claude Code moves fast —
> treat this page as a map, not a contract.

## Concept map

| Claude Code | Ava | Notes |
|---|---|---|
| Session (`claude`, `--continue`/`-c`, `--resume` picker) | Agent conversations + the task registry (`ava.tasks`) | No session picker. Every agent is a long-running process with its own dialog; spawn a fresh agent per task (`ava.agents.spawn`) instead of resuming an old session. |
| Project instructions — `CLAUDE.md`, `CLAUDE.local.md`, path-scoped `.claude/rules/` | `AGENTS.md` / `CLAUDE.md` in the repo (Ava surfaces both); standing rules in the shared memory pool as `type/feedback` notes | `@path` imports in CLAUDE.md are Claude-Code-only — Ava does not expand them; keep files self-contained. |
| Auto memory (`~/.claude/projects/<project>/memory/`, `/memory`) | Shared memory pool (`~/.ava/memory`, `ava memory` CLI) + per-agent `memory/` workspace | Notes are markdown with frontmatter and semantic search; agents write them deliberately instead of Claude Code's automatic MEMORY.md upkeep. |
| Skills (`.claude/skills/`, `~/.claude/skills/`) | Same Agent Skills `SKILL.md` standard — `ava skill install <git-url-or-path>` | Verified: unmodified Claude Code skill folders install as-is. The fields Ava does not act on are `allowed-tools` / `disallowed-tools` and `context: fork` — its single tool is `execute_code`. |
| Subagents (`.claude/agents/*.md` with tools/model/permissionMode/maxTurns frontmatter) | Peer agents — `ava.agents.spawn`, role-card skills (`be-a-<role>`), presets (`ava presets`) | Ava agents are full peers with their own workspace, memory and tools, not scoped subprocesses. Boundaries are set in the spawn prompt and recorded as `type/role` memory — there is no per-agent tool allowlist. |
| Hooks (`settings.json` → PreToolUse, PostToolUse, Stop, SessionStart, …) | No hook system. Policy rules → AGENTS.md + memory notes; conditions/notifications → `ava.watcher`; format/lint/test on change → repo CI | A hook that enforced policy (e.g. blocking `rm -rf`) must become a written rule, not an executable hook. |
| Slash commands (`.claude/commands/`, built-ins `/init`, `/compact`, `/review`) | Skills (same idea: a named instruction pack the agent reads) + the `ava` CLI for operations | No interactive slash-command bar. |
| Permission prompts & modes (default / acceptEdits / plan / `--dangerously-skip-permissions`) | No per-tool approval UI. Agents decide what to run; the user's standing gates are recorded in memory at onboarding, and irreversible / outward-facing actions raise `ava.ui.notify(require_response=True)` | The biggest behavioral difference — see pitfalls. |
| MCP (`claude mcp add`, settings.json) | Same MCP standard; `ava mcp` add/list/remove/enable/disable | Per machine: a server is registered on the machine whose tools it wraps. |
| Plugins | `ava plugins install <git-url>` — accepts Ava-native skills and Claude Code plugin packages (their `agents/` become an orchestrator skill, bundled `.mcp.json` is carried over) | |
| `/compact` | `ava.self.compact` — agent-initiated, plus automatic compaction | |
| Headless `claude -p "<task>"` | `ava.shell.run` / `ava.shell.run_background` for one-shot commands; spawn an agent for anything that needs reasoning | |

The bridge works in both directions: `ava mcp serve` exposes the Ava gateway as
an MCP server, so Claude Code can keep driving the fleet
(`claude mcp add ava -- ava mcp serve`).

## Migration steps

1. **Port the skills first** — they are the same format. `ava skill install
   <path-or-git-url>` for each `.claude/skills` directory. Check for
   `allowed-tools` / `context: fork` frontmatter; Ava ignores both.
2. **Move CLAUDE.md content.** Project instructions go to the repo's
   `AGENTS.md` (Ava surfaces it automatically). Rules about *how to work with
   the user* — language, gates, cadence — go to the shared memory pool as
   `type/feedback` notes, so every agent honors them rather than one project.
   Flatten `@path` imports.
3. **Replace subagents with roles.** Each `.claude/agents/*.md` becomes a
   role-card skill (`be-a-<role>`; see the presets sub-skill's
   reference/role-cards.md) plus a preset naming it in
   `skills_to_expand_at_start`. The spawn prompt carries the mission; record
   the role's boundary as `type/role` memory.
4. **Recreate hooks as rules and watchers.** Policy hooks become AGENTS.md /
   memory rules; notification and condition hooks become `ava.watcher`
   watchers; format/lint hooks belong in repo CI.
5. **Recreate scheduled work** as `ava schedules` (gateway-supervised cron) or
   `ava.watcher.cron`.
6. **State your gates once.** There is no permission-mode switch: tell the
   onboarding agent what must never happen without your OK. It writes the
   rules to the pool, and every later agent reads them.

## Differences and pitfalls

- **No permission prompts.** Claude Code asks before sensitive actions; Ava
  has no approval UI and will run shell commands and edit files without
  asking, governed by the gates recorded in memory and its own judgment. If
  you relied on approving every action, state your hard gates explicitly
  during onboarding — this is the single largest adjustment.
- **No session picker.** Conversation state lives with the agent process, not
  in a resumable session list. Hand off via files and the task registry
  (`ava.tasks`), not `claude --continue`.
- **`allowed-tools` is not enforced.** Every Ava agent has the full
  capability set (`execute_code` + `ava.*`). Constrain behavior with prompts,
  role cards, and memory.
- **Skills are instructions, not guarantees.** Same as Claude Code — an
  agent reads a skill and follows it as best it can; nothing enforces it.
- **Subagents become full agents.** A Claude Code subagent shares your
  project and returns to the main conversation; an Ava agent is a peer with
  its own workspace. Give it a self-contained prompt and a defined handoff
  (file path or task id).
- **Hooks have no runtime equivalent.** Anything that fired on every tool
  call must become a checked-in rule or a CI check instead.

## Sources

Official Claude Code docs, accessed 2026-08-12:

- https://code.claude.com/docs/en/overview — sessions, surfaces, permission modes, CLI flags
- https://code.claude.com/docs/en/memory — CLAUDE.md hierarchy, auto memory, `@path` imports
- https://code.claude.com/docs/en/skills — SKILL.md format, locations, frontmatter
- https://code.claude.com/docs/en/sub-agents — subagent format and frontmatter
- https://code.claude.com/docs/en/hooks — hook events and configuration
