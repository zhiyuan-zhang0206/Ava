"""The framework's own graph-edge hooks, in run order.

Compaction, crash repair, the capability-index drift check, skill recall and security-finding
delivery are core capabilities, not plugins: unconditional, and independent of the plugin enable
config. Before the LLM, repair runs before plugins so its full-history replacement cannot erase
their appended notes while committing their other state updates. The remaining framework hooks
follow plugins in their existing order. Every hook still reads the same original state; only the
order in which their returned deltas merge changes. At other edges, plugins run first.
"""

from agent.graph.hooks.capabilities import newly_installed_skills
from agent.graph.hooks.compact import compact_reminder
from agent.graph.hooks.skill_recall import SkillRecallHook
from agent.hooks import Hook
from agent.hooks.repair import repair_dangling_tool_pairing
from agent.hooks.security import deliver_security_findings
from base.packages.plugins.extensions import ExtensionRegistry, GraphHook, HookPoint


def framework_hooks() -> dict[HookPoint, tuple[Hook, ...]]:
    return {
        "after_init": (),
        "before_llm": (
            repair_dangling_tool_pairing,
            compact_reminder,
            SkillRecallHook(),
            newly_installed_skills,
        ),
        "before_exec": (),
        "after_exec": (deliver_security_findings,),
    }


def hooks_at(point: HookPoint, extensions: ExtensionRegistry) -> list[tuple[str | None, GraphHook]]:
    """Compose one edge's hooks, preserving plugin and framework relative order.

    Repair's full-history delta must precede plugin notes at before_llm. This is
    one framework invariant, not a priority system or a change to runner inputs.
    """
    framework = framework_hooks()[point]
    if point == "before_llm":
        return [
            (None, repair_dangling_tool_pairing),
            *extensions.hooks(point),
            *((None, hook) for hook in framework if hook is not repair_dangling_tool_pairing),
        ]
    return [*extensions.hooks(point), *((None, hook) for hook in framework)]
