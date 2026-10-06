"""Agent-runtime face of the ava_fleet plugin — the fleet + reduce-context-switch
prompt sections.

Loaded only in the agent process: `agent.extensions` imports this module after
`plugin.py` on the full path (host boot / graph build). The plugin's SDK
**surface** — `ava.self.set_label`, the `ava.ui` notice members,
the `ava.tasks` registry namespace, and the `agents.spawn` label wrap — lives
in `plugin.py` and loads in agent-launched children too (task #3633).
"""

from __future__ import annotations

from base.config import settings
from base.host.env.agent_slices import AgentSlices
from base.packages.plugins.extensions import PluginContributions


def _fleet_self_section(_slices: AgentSlices) -> str:
    return (
        "## Fleet\n\n"
        "Fleet provides optional labels (`ava.self.set_label`), user notices "
        "(`ava.ui.notify`), task tracking (`ava.tasks`), and peer collaboration. "
        "Workflow selection belongs to `ava-workflow`; enabling Fleet does not "
        "require delegation, a registry task, or a management tree. Agents are "
        "peers; accepted assignments determine reporting, not spawn ancestry. "
        "Before using Fleet coordination or task tracking, load `ava-fleet` "
        "and its applicable operating contract.\n\n"
        "## Agent-to-agent communication\n\n"
        "Conversation output is not a peer message: use `ava.agents.send_message` "
        "to deliver actionable updates, necessary commitments, blockers, results, "
        "or handoffs directly to whoever must act. Act on received messages "
        "without courtesy ACKs; explicit reporting agreements still apply. "
        "Use one reporter per milestone; do not relay unchanged results or "
        "duplicate task-log writes. Periodic checking does not imply periodic "
        "broadcasting, including generated watchers and schedules.\n\n"
        "For the user, reply in an active dialog; otherwise queue necessary "
        "decisions and results with `ava.ui.notify`, even while they are offline. "
        "Posting is delivery. Dismiss a pending notice when the dialog resolves "
        "it; edit or dismiss stale notices. User-only decisions reach the user "
        "directly; progress for accepted delegated work reaches its agreed "
        "delegator. With no delegator, deliver directly."
    )


def _reduce_context_switch_section(_slices: AgentSlices) -> str:
    """Keep the interruption boundary resident; load the playbook on demand."""
    if not settings.agent.reduce_context_switch:
        return ""
    return (
        "## Reduce context switch for the human\n\n"
        "Queue, never push: out-of-band interruption is reserved for irreversible "
        "risk in motion, the whole effort blocked on the human, or an explicit "
        "request to be woken. One notice per agent, updated in place; lack of "
        "acknowledgment does not justify escalation. Load "
        "`reduce-context-switch-for-human` for reporting cadence, aggregation, "
        "and push-channel procedures when needed."
    )


def contribute() -> PluginContributions:
    """What this plugin declares for the agent runtime."""
    return PluginContributions(
        system_prompt_sections=(_fleet_self_section, _reduce_context_switch_section)
    )
