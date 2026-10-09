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
- **REST**: `/api/presets` ([[gateway/routers/docs/routers.ava.okf.md|gateway router]] `presets.py`, POST 201/409). Optional stable creation keys recover the original resource; see [[gateway/routers/docs/resource-creation.ava.okf.md|creation receipts]].
- **Preset Maker**: The `presets` sub-skill of [[ava_builtins/skills/platform/ava-guide/docs/ava-guide.ava.okf.md|ava-guide]] researches role prompts, skills, and MCP sources, adapts standing instructions into a role-card skill, and saves verified reusable settings. The `/control#presets` entry opens an agent with `ava-guide:presets` preloaded; an initial request is optional.

MCP connections remain machine-level prerequisites. A preset neither installs
servers nor stores their definitions or credentials. The maker verifies the
intended machines and records those dependencies when handing over the preset.

Preset Maker uses `skill-creator`'s authoring evaluation contract: saved cases,
metrics, acceptance criteria, a baseline, and output/trace evidence assess the
complete composition before it is called validated. Evaluation files live with
the role card or in deployment storage, outside the preset config. Incomplete
or failing candidates remain drafts; the stored preset has no evaluation-status
field. Saving and resolving a preset do not prove task quality.

## Data Types
- `Preset`: id, name, label, description, config (dict), created_at, updated_at
- `PresetNotFoundError` — specified name does not exist

## What config stores
`config` is a JSON mapping per-agent config field names → values (available fields are returned by `per_agent_field_names()` in `base/config`). Two skill fields, only one of which differentiates a role:
- `skills_to_inject_into_system_prompt` — the `# Capabilities` index (name + one-line description, drill down on demand). Cluster default is `["*"]` (every loaded skill), so a list here **narrows** that agent's index. The five seeded presets carry no config at all for this reason (see the v0.1.0 baseline seed in `db/schema.sql` (the pre-release 20260731T084500 migration was squashed into it at the 2026-08-14 reset)).
- `skills_to_expand_at_start` — **Full-text preload** (system note, effective at spawn, not lost on compact); use this for discipline-like short skills (for example, a role-specific instruction pack).

## Usage
```python
from uuid import uuid4

# The preset is a key INSIDE the config overlay (task #2694):
ava.agents.spawn(prompt="...", config_overlay={"preset": "fast-worker"}, idempotency_key=str(uuid4()))
# ...optionally with explicit overrides that win per key:
ava.agents.spawn(
    prompt="...",
    config_overlay={"preset": "fast-worker", "llm_model": "deepseek-flash"},
 idempotency_key=str(uuid4()))
```
`preset` is the base, the explicit overlay fields are the precise override
(per key). The former top-level `spawn(preset=...)` argument is retired
(task #4086). The spawn boundary resolves the preset; the agent row stores the
resolved overlay plus `agents_meta.preset_name`, and the inspector shows the
preset reference plus only the fields that differ from the preset (diff
display).

**Fork rule:** a fork keeps the source agent's effective config so the
inherited context stays cache-valid; only ADDING skills to
`skills_to_inject_into_system_prompt` / `skills_to_expand_at_start` is allowed
(supersets; anything else → `fork_config_change_not_allowed`). Added skills
load at the context tail.

## Key Dependencies
- [[ava/agents/docs/agents.ava.okf.md]] — spawn resolves `config_overlay.preset` at the spawn boundary
- [[ava/skills/docs/skills.ava.okf.md|Skill System]] — name resolution for the two skill combination fields + index-vs-expand mechanism

## Notes
When presets ≤ 10, no search/filter is provided—agent code does it itself.
