"""The framework's own graph-edge hooks, in run order.

Compaction, crash repair, the capability-index drift check and security-finding delivery are core
capabilities, not plugins: unconditional, and independent of the plugin enable config. They run
after every plugin hook at the same edge. Repair comes first among them because it guards the message history every hook
after it (compact's force-compact summarization) may feed to an LLM; the capability-index drift
check comes last because it appends a note to whatever history survives repair's guard and
compact's possible full replacement.
"""

from agent.hooks._registry import Hook
from agent.hooks.capabilities import _newly_installed_skills
from agent.hooks.compact import _compact_reminder
from agent.hooks.repair import _repair_dangling_tool_pairing
from agent.hooks.security import _deliver_security_findings
from base.packages.plugins.extensions import HookPoint


def framework_hooks() -> dict[HookPoint, tuple[Hook, ...]]:
    return {
        "after_init": (),
        "before_llm": (_repair_dangling_tool_pairing, _compact_reminder, _newly_installed_skills),
        "before_exec": (),
        "after_exec": (_deliver_security_findings,),
    }
