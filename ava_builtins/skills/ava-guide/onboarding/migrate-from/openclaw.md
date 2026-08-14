# Migrating from OpenClaw

OpenClaw is the open-source personal AI assistant (MIT) that runs on your own
devices and meets you in the chat channels you already use — WhatsApp,
Telegram, Slack, Discord, Signal, iMessage and more. Originally built for
"Molty" (formerly Clawdbot / Moltbot), it is now developed under the
non-profit OpenClaw Foundation. Its core is the **Gateway**: a self-hosted
control plane for sessions, tools, events and channel connections.

> Snapshot as of August 2026, from the official docs. Treat this page as a
> map, not a contract.

## Concept map

| OpenClaw | Ava | Notes |
|---|---|---|
| Gateway (single source of truth for sessions / routing / channels) | Ava cluster — gateway + agent runners; `ava cluster` / `ava status` | |
| Channels (WhatsApp, Telegram, Slack, Discord, Signal, iMessage, …) with pairing (`openclaw pairing approve`) | Platform dialog + `ava.ui.notify`; clusters running the IM Bridge add Telegram | Ava is not a chat bot for your messenger. The interaction surface is the platform UI/dialog plus the notice queue; no per-channel pairing exists because there are no channels. |
| Sessions (per sender / workspace; DMs share the main session) | Agent dialogs + the task registry (`ava.tasks`) | |
| Agents (multi-agent routing) | Peer agent fleet (`ava.agents.spawn`), delegation via messages and tasks | |
| Skills / tools / plugins (ClawHub) | `ava skill install` (Agent Skills standard), `ava plugins`, MCP via `ava mcp` | Skills already in SKILL.md form install directly; anything else needs porting to SKILL.md or an MCP server covering the same tool. |
| Cron / webhooks | `ava schedules` (gateway-supervised) + `ava.watcher` | |
| Config `~/.openclaw/openclaw.json` (JSON5: channels, messages, providers, security) | `ava config get/set/unset` + `ava presets` | |
| Control UI (`openclaw dashboard`, :18789) | Platform fleet view; per-report pages via `ava.ui.serve` | |
| Companion apps (voice, canvas, camera, screen sharing) | No equivalent — Ava delivers pages (`ava.ui.serve`) and text | |
| Memory | Shared memory pool (`~/.ava/memory`) + per-agent memory | |
| Self-hosted, single operator | Multi-machine cluster; agents can be spawned on any enrolled machine | |

## Migration steps

1. **Resize the surface expectation.** Ava reaches you in the platform dialog
   and the notice queue (plus Telegram where the IM Bridge runs).
   WhatsApp/Slack/Discord channels do not port — say so before the user goes
   looking for the bot.
2. **Recreate the automations.** Cron entries → `ava schedules`; webhook
   triggers → a small script behind a schedule, or a `ava.watcher` condition.
3. **Port capabilities.** SKILL.md-format skills install directly; other
   OpenClaw skills/plugins need rewriting as Agent Skills, or finding an MCP
   server that covers the tool.
4. **Recreate roles.** OpenClaw "agents" → Ava role agents: spawn one per
   domain, record each boundary as `type/role` memory.
5. **Move provider/model choices** into presets per role (OpenClaw's
   `providers` block has no single Ava equivalent).

## Differences and pitfalls

- **Chat-channel-first vs platform-first.** The biggest shift. There is no
  pairing, no `allowFrom` allowlists, no group-mention routing — the platform
  handles identity, the dialog is where live work happens, and notices are
  the async channel.
- **No companion apps.** Voice, canvas, camera, and screen-sharing surfaces
  do not exist in Ava. Reports and interactive pages are the replacement
  (`ava.ui.serve`).
- **The security model moves.** OpenClaw treats inbound channel messages as
  untrusted and sandboxes what tools can do; Ava's inbound surface is the
  platform itself, and an agent's shell actions run with the process's own
  permissions on the host. Scope agents by role boundaries and memory gates
  instead of channel allowlists.
- **Single operator vs fleet.** OpenClaw is one assistant you talk to; Ava is
  many agents working in parallel, each with a task and a delegator. The "one
  bot" mental model becomes "one fleet" — you talk to whichever agent owns
  the domain.

## Sources

Official OpenClaw docs, accessed 2026-08-12:

- https://github.com/openclaw/openclaw — README: architecture, install, pairing, security model
- https://docs.openclaw.ai/ — gateway / channels / agents / capabilities / config reference
