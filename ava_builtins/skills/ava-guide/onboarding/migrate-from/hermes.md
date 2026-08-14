# Migrating from Hermes Agent

Hermes Agent is Nous Research's open-source agent framework: an autonomous,
self-improving agent that runs anywhere from a local machine to serverless
backends, reachable through 20+ messaging platforms. Its signature is a closed
learning loop — agent-curated memory, autonomous skill creation and
self-improvement, dialectic user modeling (Honcho) — plus model freedom: any
provider, Mixture-of-Agents presets, and fallback chains.

> Snapshot as of August 2026 (v0.19 line), from the official docs. Treat this
> page as a map, not a contract.

## Concept map

| Hermes | Ava | Notes |
|---|---|---|
| Profiles (`hermes profile`) | Presets (`ava presets`) + per-spawn `config_overlay` | |
| Sessions (`hermes sessions`, FTS5 cross-session recall) | Agent conversations + memory-pool semantic search (`ava.memory.search`) | |
| Memory (agent-curated, periodic nudges) | Shared memory pool + per-agent `memory/`; daily consolidation job (`ava memory`) | Ava has no runtime "nudge to persist" loop — memory hygiene is a standing duty written into agent prompts, enforced by scheduled consolidation. |
| Skills — autonomous creation, self-improvement, Skills Hub (`hermes skills` / `curator`) | Skills are files installed via `ava skill install`; changing a skill is a PR, not a runtime loop | Honest gap: no autonomous skill creation or in-use self-improvement. The closest relative is the weekly self-evolution pass that mines agent traces and proposes skill edits — reviewed, not autonomous. |
| `hermes bundles` (skill groups) | N/A | |
| SOUL.md persona | System prompt + role-card skills (`be-a-<role>`) | |
| Channels (CLI, Telegram, Discord, Slack, WhatsApp, Signal, Email, SMS, …) | Platform dialog + `ava.ui.notify`; Telegram via the IM Bridge where it runs | |
| Command approval (`hermes approvals`) | Memory gates + `ava.ui.notify(require_response=True)` | No approval-history ledger. |
| Terminal backends (local, Docker, SSH, Daytona, Modal) | Multi-machine agents (`ava.agents.spawn(machine=...)`), `ava.shell.sessions`, containers via shell | Daytona/Modal serverless hibernation has no Ava equivalent. |
| `hermes cron` | `ava schedules` + `ava.watcher` | |
| MCP (`hermes mcp`) | `ava mcp` | |
| Model flexibility (`hermes model`, `hermes moa`, `hermes fallback`) | Presets + `config_overlay` pick one model per agent | No Mixture-of-Agents, no provider failover chain. The workaround is delegation: different agents on different models. |
| `hermes import-agent` (imports a `~/.claude` or `~/.codex` setup) | This page set — same job, done by hand | |
| `hermes claw` (OpenClaw migration helpers) | See [openclaw.md](openclaw.md) | |

## Migration steps

1. **Port the skills you wrote or installed** — SKILL.md-format ones install
   with `ava skill install`. Skills Hermes learned on its own exist only in
   its memory; recreate the ones worth keeping as hand-written skills.
2. **Write the persona as a role card.** SOUL.md → a `be-a-<role>` skill (see
   the presets sub-skill), plus a preset that names it in
   `skills_to_expand_at_start`.
3. **Seed the memory pool.** Hermes's learned user model does not transfer;
   let onboarding write the `type/user` and `type/feedback` notes from the
   interview — the pool is the replacement for Honcho-style user modeling.
4. **Recreate schedules** (`hermes cron` → `ava schedules` / `ava.watcher`).
5. **Choose models per role.** `hermes model` / MoA presets → one model per
   agent via presets; no multi-model aggregation.
6. **Move channel habits.** Pick the platform dialog as the primary surface;
   there is no WhatsApp/Discord/Slack bot (Telegram via the IM Bridge only,
   where configured).

## Differences and pitfalls

- **No closed learning loop.** Hermes's headline feature — the agent writes
  its own skills from experience and improves them in use — has no Ava
  counterpart. In Ava the loop is social, not automatic: an agent notices
  what should become a skill, and the change lands via PR and review. Expect
  less autonomy, more auditability.
- **No model mixing.** `hermes moa` (Mixture-of-Agents presets) and `hermes
  fallback` chains do not exist; each agent runs one model, chosen at spawn.
- **No approval history.** `hermes approvals` lets you mine what was
  approved; Ava's gates are memory rules and `require_response` notices, with
  no such ledger.
- **Channels shrink to one surface.** 20+ platforms become the platform
  dialog + notices (+ Telegram IM Bridge). Set that expectation early.
- **Sessions are agent-scoped.** `hermes sessions` browse/export/prune has no
  equivalent UI; conversation state lives with each agent, and handoffs go
  through files and tasks.

## Sources

Official Hermes Agent docs, accessed 2026-08-12:

- https://hermes-agent.nousresearch.com/docs/ — overview, install, concepts, learning loop
- https://github.com/NousResearch/hermes-agent — website/docs/reference/cli-commands.md (CLI reference), releases (v0.19 line)
