# ava:ref agent/graph/prompt/docs/composition.ava.okf.md#codeact-batching
from base.host.env.agent_slices import AgentSlices


def codeact_section(slices: AgentSlices) -> str:
    if not slices.prompt.prompt_codeact_enabled:
        return ""
    return (
        "# CodeAct — batch work into fewer calls\n\n"
        "Emit at most one `execute_code(code: str)` tool call per turn; never "
        "split one turn's work across parallel `execute_code` calls. Combine "
        "known operations inside that single script when you do not need to "
        "inspect an intermediate result:\n\n"
        "- Read independent files together.\n"
        "- Use ordinary if-else logic for branches whose conditions and "
        "actions are already understood.\n"
        "- Combine fetch, transform and write when the transformation is "
        "known and no intermediate review is required."
    )
