---
name: presets
description: Create, update, and manage agent config presets — named, reusable config-overlay templates. Use when the user wants a new agent type (a new role), to add a preset, or to adjust an existing preset's config.
---

# Presets — Managing Agent Config Templates

A **preset** is a named, reusable agent config template — it bundles model
choice, plugin config fields, and per-agent settings into a config overlay.
Selecting a preset at spawn time seeds the new agent's config from it; an
explicit config passed alongside wins per-key.

Use this when:
- The user asks to "add a new agent type" or "create a preset"
- An existing preset's config needs updating
- You need to understand what config a preset carries

When the "new agent type" is a **role** — a product manager, a growth lead, an
editor — the preset is the small half of the job. Read
[reference/role-cards.md](reference/role-cards.md) first: the role itself is
authored as a skill, and the preset only names it.

## Concept Review

The core problem presets solve: when spawning an agent, you don't need to
manually write config every time. Store a set of commonly used configurations
as a template, and just reference it by name when spawning.

**Preset vs `config_overlay`:** preset is the **base**, `config_overlay` is
the **precise override** at spawn time. When both are passed, `config_overlay`
fields with the same name override those from the preset.

**What does Config store?** The preset's `config` is a JSON object whose
fields are per-agent config field name → value. Available per-agent fields are
returned by `shared/config`'s `per_agent_field_names()`, and common ones
include:

| Field | Description | Type |
|------|------|------|
| `llm_model` | Model selection | string |
| `skills_to_inject_into_system_prompt` | The `# Capabilities` index (name + one-line description, drill down on demand). Cluster default `["*"]` = every loaded skill, so a list here **narrows** this agent's index | list[string] |
| `skills_to_expand_at_start` | List of skills to preload in full text (as system note, effective from spawn and not lost on compact) | list[string] |
| `reasoning_effort` | Reasoning effort level | string |
| `compact_reminder_fraction` | Compact reminder threshold | float |
| `auto_compact_fraction` | Auto compact threshold (as fraction of window) | float |
| `auto_compact_ceiling_tokens` | Absolute cap on auto compact threshold in tokens (0=no cap; actual threshold is the smaller of the two) | int |
| `passive_memory_recall_enabled` | Enable passive memory recall | bool |
| `agent_reply_reminder_cadence` | Reply reminder cadence | int |

## Process

### 1. Clarify Requirements

Confirm three things with the user:

1. **What does this agent do?** A one-sentence description of its role and task domain.
2. **What does it need beyond the defaults?** Every agent already sees the full skill index, so the question is not "which skills" — it is which few skills this role must have *read in full* before its first turn, plus any model / effort / behavior setting the role depends on.
3. **Will this combination be reused?** A one-off is a `config_overlay` at spawn, not a preset.

### 2. Design Config

Based on the user's description, assemble a config object:

```python
config = {
    # Full SKILL.md text loaded before the first turn — the field that actually
    # differentiates a role. Keep it to short disciplinary skills; a large
    # reference skill is already one ava.help() away via the index.
    "skills_to_expand_at_start": ["ava-code.conventions"],
    "llm_model": "claude-sonnet-5",
}
```

**Do not** hand a preset a `skills_to_inject_into_system_prompt` list unless the
intent is to SHORTEN what that agent reads: the cluster default is `*` (every
loaded skill is indexed), so any explicit list narrows the index. It hides the
rest of the catalog from the listing only — `ava.help(ava.skills)` still
enumerates everything and any skill loads by name — so this buys attention, not
a capability boundary.

### 3. Create Preset

Create via REST API:

```python
import os, httpx

base = os.environ.get("AVA_GATEWAY_URL", "http://localhost:8000")
body = {
    "name": "my-preset",           # kebab-case, unique identifier
    "label": "My Preset",          # Human-readable name
    "description": "What this preset is for",
    "config": {"llm_model": "claude-sonnet-4-5-20250929", ...}
}
r = httpx.post(
    f"{base}/api/presets", json=body,
    headers={"Authorization": f"Bearer {os.environ['AVA_CLUSTER_SECRET']}"}
)
print(r.status_code, r.text)  # 201 on success; 409 = name taken
```

Or use the CLI:

```bash
ava presets create --name my-preset --label "My Preset" \
    --description "What this preset is for" \
    --config '{"llm_model":"claude-sonnet-4-5-20250929"}'
```

### 4. Verify

After creation, verify that the preset is usable:

```bash
# List all presets
ava presets ls

# View a specific one
ava presets get my-preset

# (Optional) spawn an agent to test
# In the SDK: ava.agents.spawn(prompt="hello", preset="my-preset")
```

## CLI Operations Reference

| Operation | Command |
|------|------|
| List all | `ava presets ls` |
| View one | `ava presets get <name-or-id>` |
| Create | `ava presets create --name <n> --label <l> [--description <d>] [--config <json>]` |
| Update | `ava presets update <name-or-id> [--name <n>] [--label <l>] [--description <d>] [--config <json>]` |
| Delete | `ava presets delete <name-or-id>` |

## Reference: Existing Presets

Run `ava presets ls` or `ava.agents.presets.list()` to view presets in the
current cluster. The seeded roles — **coder**, **reviewer**, **researcher**,
**orchestrator**, **explorer** — currently carry an empty config: their old
skill lists became narrowing once the index went universal and were removed, and
what differentiates them is still to be written.

## Design Principles

1. **Limit the number of presets.** The value of a preset lies in reuse. If
   a config is only used once, spawning with `config_overlay` directly is
   more straightforward.
2. **The index is not what a preset picks.** Every agent already sees every
   loaded skill in `# Capabilities` (cluster default `*`). So
   `skills_to_inject_into_system_prompt` in a preset can only *subtract*, and
   subtracting only shortens the listing — `ava.help(ava.skills)` still
   enumerates the full catalog and an unlisted skill still loads by name. Use it
   to keep a narrow role's index readable, never to "give" a role its skills and
   never as a way to withhold one.
3. **What a preset differentiates is what is read before turn one.**
   `skills_to_expand_at_start` preloads full SKILL.md text at spawn and
   re-injects it after every compact — for short rules that must be active from
   the start and must not be lost. Everything else stays index-only and is one
   `ava.help(ava.skills.<name>)` away; preloading a large reference skill just
   buys tokens.
4. **If there is no config field to carry it, just create a small skill.**
   If a preset needs to inject a set of instructions that has no existing
   config field to hold it, don't force it elsewhere — create a new small
   skill and preload it. This is cleaner than piling onto config and is also
   reusable.
