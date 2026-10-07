"""CodeAct batching section — default-on system-prompt guidance.

Owned as its own module (like `capabilities.py`) because the section list in
`system_prompt.py` is at its line ceiling; `system_prompt` imports and
registers the section explicitly so the render order stays its reading order.
"""

from base.host.env.agent_slices import AgentSlices


def _codeact_section(slices: AgentSlices) -> str:
    """Opt-in batching guidance; preserve intermediate review when it is needed."""
    if not slices.prompt.prompt_codeact_enabled:
        return ""
    return (
        "# CodeAct — batch work into fewer calls\n\n"
        "Use one `execute_code(code: str)` call for several known operations "
        "when you do not need to inspect an intermediate result:\n\n"
        "- Read independent files together.\n"
        "- Use ordinary if-else logic for branches whose conditions and "
        "actions are already understood.\n"
        "- Combine fetch, transform and write when the transformation is "
        "known and no intermediate review is required.\n\n"
        "Split calls when a result determines the next decision, when you "
        "need approval, or when execution and output limits would hide "
        "evidence you need. Batching should reduce unnecessary calls while "
        "keeping failures and partial effects clear."
    )
