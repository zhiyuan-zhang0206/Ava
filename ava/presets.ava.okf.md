---
type: doc
title: ava.agents.presets — Configuration Presets
description: '`ava.agents.presets` manages named, reusable agent configuration templates. Specify a preset during spawn to quickly load a set of configurations (model, plugin, skill, etc.).'
tags:
- agent-view
- sdk
- agent-lifecycle
---

# ava.agents.presets — Configuration Presets

## What it is

`ava.agents.presets` manages named, reusable agent configuration templates. Specify a preset during spawn to quickly load a set of configurations (model, plugin, skill, etc.).

## SDK Surface (read-only)

`ava.agents.presets` intentionally **only exposes read** operations:
- `list() → list[Preset]` — List all presets, sorted by name.
- `get(name) → Preset` — Get the preset with the given name. Throws `PresetNotFoundError` if not found.

CRUD (create / update / delete) **is not in the SDK**—presets are operational configuration assets, not something agents frequently modify during turns, so the write side is left to CLI and REST, and the SDK only provides read operations needed for spawn.

## Write Side: CLI / REST / Guide Sub-skill
- **CLI**: `ava presets ls / get / create / update / delete` (`--name` / `--label` / `--description` / `--config <json>`).
- **REST**: `/api/presets` ([[gateway/routers/routers.ava.okf.md|gateway router]] `presets.py`, POST 201/409).
- **playbook**: Turn the user's request for "a new agent type / add a preset" into a preset operational manual in the `presets` sub-skill of [[ava_builtins/skills/ops_lifecycle/ava-guide.ava.okf.md|ava-guide]] (d31660c8).

## Data Types
- `Preset`: id, name, label, description, config (dict), created_at, updated_at
- `PresetNotFoundError` — specified name does not exist

## What config stores
`config` is a JSON mapping per-agent config field names → values (available fields are returned by `per_agent_field_names()` in `shared/config`). Two skill fields, only one of which differentiates a role:
- `skills_to_inject_into_system_prompt` — the `# Capabilities` index (name + one-line description, drill down on demand). Cluster default is `["*"]` (every loaded skill), so a list here **narrows** that agent's index. The five seeded presets carry no config at all for this reason (see the v0.1.0 baseline seed in `db/schema.sql` (the pre-release 20260731T084500 migration was squashed into it at the 2026-08-14 reset)).
- `skills_to_expand_at_start` — **Full-text preload** (system note, effective at spawn, not lost on compact); use this for discipline-like short skills (e.g., `ultra-speed-worker` preset loads ava-ultra-speed).

## Usage
```python
ava.agents.spawn(prompt="...", preset="fast-worker")
# or
preset = ava.agents.presets.get("fast-worker")
ava.agents.spawn(prompt="...", config_overlay=preset.config)
```
`preset` is the base, `config_overlay` is the precise override; when both are passed, `config_overlay` overrides field by field.

## Key Dependencies
- [[agents.ava.okf.md]] — spawn accepts preset parameter
- [[ava/skills.ava.okf.md|Skill System]] — name resolution for the two skill combination fields + index-vs-expand mechanism

## Notes
When presets ≤ 10, no search/filter is provided—agent code does it itself.
