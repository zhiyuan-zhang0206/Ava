"""Agent config — AgentSettings.

Split out of the former flat Settings god object; each field keeps its exact
env alias so the .env surface is unchanged. Aggregated by shared/config.

The AgentSettings schema is split by field domain into five sub-models
(AgentRuntimeSettings, AgentPromptSettings, AgentMemorySettings,
AgentCompactionSettings, AgentEvalSettings) and composed here by pydantic
multiple inheritance, so the `settings.agent.*` aggregation surface is
unchanged: every consumer sees the same flat field set on one instance.
pydantic orders `model_fields` in reverse MRO, so the bases are declared in
reverse display order (Eval -> Compaction -> Memory -> Prompt -> Runtime) to
keep the config-panel field order as close to the historical one as possible.
"""

from __future__ import annotations

from shared.config.agent_compaction import AgentCompactionSettings
from shared.config.agent_eval import AgentEvalSettings
from shared.config.agent_memory import AgentMemorySettings
from shared.config.agent_prompt import AgentPromptSettings
from shared.config.agent_runtime import AgentRuntimeSettings


class AgentSettings(
    AgentEvalSettings,
    AgentCompactionSettings,
    AgentMemorySettings,
    AgentPromptSettings,
    AgentRuntimeSettings,
):
    """Aggregate of the agent-domain schema blocks (composition via inheritance).

    The per-domain sub-models in `agent_*.py` each inherit `EnvSettings` and
    populate from the flat `os.environ` through their fields' env aliases;
    this class merges their fields into the single `settings.agent` schema.
    """
