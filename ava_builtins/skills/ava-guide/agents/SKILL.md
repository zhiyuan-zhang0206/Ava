---
name: agents
description: Explains Ava agent lifecycle, commands, presets, schedules, and config overlays. Use when creating or managing agents, defining user commands, scheduling work, changing agent configuration, or clarifying how these concepts differ.
---

# Agent-Level Concepts

This sub-skill covers the concepts that live at the agent layer: agents
themselves, commands, presets, and schedules.

## Agents (running AI process)

An **agent** is a running AI process — you are one. The lifecycle verbs:

- `spawn` — start a brand-new agent.
- `fork_from` — copy another agent's conversation state into a new one.
- `terminate` — end it (default: finish current turn first; `force=True` kills
  the process immediately).
- `restart` — finish current turn, then restart as a fresh process under the
  same `agent_id`.
- `resurrect` — wake a `terminated` agent with its full conversation state
  intact (must include a `prompt` telling it why it was woken).

State machine: `idling (unclaimed) → running ⇄ idling → restarting → terminated`.

Agent-to-agent communication: `ava.agents.send_message(agent_id, content)` —
pure INSERT, no status check, no wait, no delivery receipt. If the target is
`terminated`, it is auto-resurrected to handle the message. Use
`get_neighbors` to see which agents are most closely tied to you (ties form on
spawn/fork/resurrect/send_message and decay over time), and `get_ancestors`
to see the spawn chain above an agent (who spawned whom, nearest first —
the responsibility-attribution read).

Each agent can have a **label** (`ava.self.set_label` / `get_label`) — a
human-readable role/name that persists once set.

SDK reference: `ava.agents` (`spawn` / `terminate` / `restart` / `resurrect` /
`send_message` / `list_agents` / `list_machines` / `get_neighbors` /
`get_ancestors` / `commands` / `get_last_message`).

## Command (user-level command)

A **command** is a `/name <natural language instruction>` — issued from the
frontend Composer or via `send_message` between peers. `expand_command` expands
it into the prompt the model actually sees; the model never sees the `/`-trigger
process, only the expanded text. Commands are addressable prompt functions —
agents can use `ava.agents.send_message(peer, "/name ...")` to send a named
intent with a description.

Two sources:**built-in commands** (`commands/*.md` + `~/.ava/commands/*.md` for
user overrides) and **plugin commands** (plugins carrying a `commands/` directory,
exposed as `/plugin.name`). Every active skill also gets a free eponymous command
(calling it loads the skill via `ava.help(ava.skills.<name>)`).

Create a command by placing a `<name>.md` file with optional frontmatter
(`description` + `instruction-hint`) and a fixed prompt template as the body. One
command accepts one argument — the free text after `/`.

Commands are **user-level** operations ("what should the agent do this
conversation"), distinct from the `ava` CLI (ops-level, runs in shell, operates
the deployment itself). Peers discover each other's commands via the read-only
`ava.agents.commands()` (name + description + instruction-hint, no body).

## Preset (Agent configuration template)

A **preset** is a named, reusable agent config template — bundles model choice,
plugin config fields, and other per-agent settings into a config overlay, so
spawning an agent picks a preset instead of hand-writing config each time.

SDK usage: `ava.agents.presets.list()` lists all presets,
`ava.agents.presets.get(name)` fetches one (raises `PresetNotFoundError` if
missing). Spawn with `ava.agents.spawn(..., preset="name")` — the preset is
resolved into config on the gateway side.

Preset vs `config_overlay`: preset is the **base**, `config_overlay` is the
**precise override** — when both are passed to `spawn`, fields in
`config_overlay` override same-named fields in the preset. Preset values are
validated only at child startup; only the explicit `config_overlay` is validated
locally at spawn time.

### Managing presets

Four paths to manage presets:

| Path | Use case |
|---|---|
| **CLI** `ava presets` | Shell-level CRUD — list, get, create, update, delete. Best for operators and agents that prefer a single command over HTTP calls. |
| **REST API** `/api/presets` | Programmatic CRUD from schedules or scripts (GET list/get, POST create, PATCH update, DELETE). |
| **Frontend** `/control/presets` | Web UI table with create/edit/delete forms. |
| **SDK** `ava.agents.presets` | In-agent code: `list()` / `get(name)` for reading (write ops go through CLI or API — the SDK is read-only for presets). |

To create a preset from scratch (translating user needs → skill combination →
preset), load `ava.skills.presets` (the [presets sub-skill](../presets/SKILL.md)).

## Schedules (time-based / condition-triggered persistent tasks)

The gateway hosts **schedules**: scripts written by agents, run persistently in
a session, auto-restarted on crash (time-triggered, event/threshold-triggered, or
both). Managed via REST API (`/api/schedules`) or the frontend
`/control/schedules` page.

To turn a natural-language scheduling need into a schedule, load
`ava.skills.ava_schedule_writer` and follow it (it clarifies trigger/skip/error
handling, writes a resumable script, then `POST /api/schedules` to create).


## Config adjustment (modifying system configuration)

Ava's config surface has four layers, each with its own adjustment path. Know
which layer you're touching before picking the tool.

### The four config layers

| Layer | What it controls | Where it lives |
|---|---|---|
| **Cluster / host config** | Model keys, DB URLs, service ports, timezone, feature flags — the `.env` fields. | `$AVA_HOME/.env` (gateway's for cluster fields; each host's for host fields) |
| **Agent per-agent config** | Per-agent overrides: model, skills, reasoning effort — the fields a spawn/restart overlay carries. | Passed at spawn (preset or `config_overlay`), snapshotted into the agent row |
| **Presets** | Named bundles of per-agent config — templates for spawn. | `agent_presets` DB table |
| **User settings** | Frontend display preferences (timeline density, theme, etc.) — per-user, synced across frontends. | `user_settings` DB table (TanStack Query + `useUserSettings`) |

### Adjustment paths by layer

#### Cluster / host config

| Path | Command / URL | Notes |
|---|---|---|
| **CLI** | `ava config get [KEY] [--machine M]` | Read all or one field |
| | `ava config set KEY=VALUE [...] [--machine M]` | Write; restart hint printed |
| | `ava config unset KEY [...] [--machine M]` | Revert to default |
| **REST API** | `GET/PUT /api/config[?machine=M]` | Same surface the CLI uses; reducer semantics (absent key = left untouched, null = unset) |
| **Frontend** | `/control/config` | Machine selector + capability sections; bool toggles, text edits, sensitive-field masking |
| **Direct .env edit** | Edit `$AVA_HOME/.env` then restart | Last resort; prefer `ava config set` which validates + routes correctly |

`--machine` targets a remote agent-runner's host fields; omit for the
cluster/gateway view.

#### Agent per-agent config

Passed at spawn time — not edited on a running agent:

```python
# Via preset (base layer)
ava.agents.spawn(prompt="...", preset="coder")

# Via config_overlay (precise override)
# model id from the registry roster — see the models sub-skill
ava.agents.spawn(prompt="...", config_overlay={"llm_model": "gemini-3.7-flash"})

# Both (overlay wins per-key)
ava.agents.spawn(prompt="...", preset="coder",
                 config_overlay={"llm_model": "claude-opus-5"})
```

Per-agent config is snapshotted into the agent row at spawn; changing a preset
after spawn does NOT affect agents already spawned from it.

To change an agent's config after spawn: restart it with a new `config_overlay`
(`ava.agents.restart(agent_id)` in SDK, or `ava agents restart <id>` in CLI —
the restart path accepts a config_overlay).

#### Presets

| Path | Command / URL |
|---|---|
| **CLI** | `ava presets ls` / `get <id>` / `create --name ...` / `update <id> ...` / `delete <id>` |
| **REST API** | `GET/POST /api/presets`, `GET/PATCH/DELETE /api/presets/{id}` |
| **Frontend** | `/control/presets` |
| **SDK** | `ava.agents.presets.list()` / `.get(name)` (read-only) |

#### User settings

User settings are frontend-only (display preferences, not cluster config).
Managed via the settings panel in the web UI; synced through TanStack Query
(`useUserSettings` hook, `user_settings` DB table). Not accessible from CLI or
SDK.
