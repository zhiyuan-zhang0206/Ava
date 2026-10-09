# Preset configuration and operations

Read this when selecting config fields, resolving spawn or fork behavior, or
saving a preset through CLI / REST. Evaluation and composition belong to
[Preset Maker](../SKILL.md).

## Concept Review

The core problem presets solve: when spawning an agent, you don't need to
manually write config every time. Store a set of commonly used configurations
as a template, and just reference it by name when spawning.

**Preset vs `config_overlay`:** preset is the **base**, the explicit
`config_overlay` fields are the **precise override** at spawn time — fields
with the same name override those from the preset. On the wire the preset is a
key inside the overlay: `config_overlay={"preset": "name", "llm_model": ...}`.

**Fork rule:** a fork keeps the source agent's effective config so its
inherited context stays cache-valid. At fork, the only allowed config change
is ADDING skills to `skills_to_inject_into_system_prompt` /
`skills_to_expand_at_start` (supersets; anything else is rejected with
`fork_config_change_not_allowed`). Added skills load at the context tail.

**What does Config store?** The preset's `config` is a JSON object whose
fields are per-agent config field name → value. Available per-agent fields are
returned by `base/config`'s `per_agent_field_names()`, and common ones
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
| `agent_reply_reminder_cadence` | Peer-reply reminder cadence (`once_per_compaction` / `every_time`) | string |
| `agent_communication_style` | Feedback style (`oriented` / `concise` / `silent` / `off`) | string |

> **Model ids come from the registry, not from memory.** `llm_model` values must
> be ids on the current roster — list them with `GET /api/models` or read
> `base/lm/catalog/__init__.py` (`ModelCatalog.models` / `supported_models`); a name copied from an
> old doc or spawn may be stale or unregistered (see the
> [models sub-skill](../../models/SKILL.md)).

## Save through CLI or REST

Create via REST API:

```python
import os, httpx

# candidate_config is the complete config that passed the saved evaluation.
base = os.environ["AVA_GATEWAY_URL"].rstrip("/")
body = {
    "name": "my-preset",           # kebab-case, unique identifier
    "label": "My Preset",          # Human-readable name
    "description": "What this preset is for",
    "config": candidate_config,
}
r = httpx.post(
    f"{base}/api/presets", json=body,
    headers={"Authorization": f"Bearer {os.environ['AVA_API_TOKEN']}"}
)
r.raise_for_status()  # 201 on success; 409 = name taken
print(r.json())
```

Or use the CLI:

```bash
ava presets create --name my-preset --label "My Preset" \
    --description "What this preset is for" \
    --config '{"llm_model":"deepseek-flash"}'
```

## CLI Operations Reference

| Operation | Command |
|------|------|
| List all | `ava presets ls` |
| View one | `ava presets get <name-or-id>` |
| Create | `ava presets create --name <n> --label <l> [--description <d>] [--config <json>]` |
| Update | `ava presets update <name-or-id> [--name <n>] [--label <l>] [--description <d>] [--config <json>]` |
| Delete | `ava presets delete <name-or-id>` |
