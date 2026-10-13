"""The framework's own graph-edge hooks, in run order.

Compaction, crash repair, the capability-index drift check, skill recall and security-finding
delivery are core capabilities, not plugins: unconditional, and independent of the plugin enable
config. They run after every plugin hook at the same edge. Repair comes first among them because
it guards the message history every hook after it (compact's force-compact summarization) may
feed to an LLM; skill recall and the capability-index drift check come last (drift last of all)
because they append a note to whatever history survives repair's guard and compact's possible
full replacement.
"""

from agent.graph.hooks.capabilities import newly_installed_skills
from agent.graph.hooks.compact import compact_reminder
from agent.graph.hooks.skill_recall import SkillRecallHook
from agent.hooks import Hook
from agent.hooks.repair import repair_dangling_tool_pairing
from agent.hooks.security import deliver_security_findings
from base.packages.plugins.extensions import HookPoint


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
