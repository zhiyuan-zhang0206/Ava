from __future__ import annotations

from base.host.env.agent_slices import AgentSlices
from base.lm.catalog import ModelCatalog
from base.packages.plugins.config_registration import get_plugin_config
from base.packages.plugins.extensions import PluginContributions
from meiva_builtins.plugins.meiva_fleet.default_config import FleetConfig


def fleet_self_section(_slices: AgentSlices, *, catalog: ModelCatalog) -> str:
    del catalog
    return (
        "## Fleet\n\n"
        "Fleet provides optional labels (`meiva.self.set_label`), user notices "
        "(`meiva.ui.notify`), task tracking (`meiva.tasks`), and peer collaboration. "
        "Workflow selection belongs to `meiva-workflow`; enabling Fleet does not "
        "require a registry task or a management tree. Agents are "
        "peers; accepted assignments determine reporting, not spawn ancestry. "
        "Before using Fleet coordination or task tracking, load `meiva-fleet` "
        "and its applicable operating contract.\n\n"
        "## Agent-to-agent communication\n\n"
        "Conversation output is not a peer message: use `meiva.agents.send_message` "
        "to deliver actionable updates, necessary commitments, blockers, results, "
        "or handoffs directly to whoever must act. Act on received messages "
        "without courtesy ACKs; explicit reporting agreements still apply. "
        "Use one reporter per milestone; do not relay unchanged results or "
        "duplicate task-log writes. Periodic checking does not imply periodic "
        "broadcasting, including generated watchers and schedules.\n\n"
        "For the user, reply in an active dialog; otherwise queue necessary "
        "decisions and results with `meiva.ui.notify`, even while they are offline. "
        "Posting is delivery. Dismiss a pending notice when the dialog resolves "
        "it; edit or dismiss stale notices. User-only decisions reach the user "
        "directly; progress for accepted delegated work reaches its agreed "
        "delegator. With no delegator, deliver directly."
    )


def reduce_context_switch_section(slices: AgentSlices, *, catalog: ModelCatalog) -> str:
    del catalog
    if not get_plugin_config("meiva_fleet", slices, FleetConfig).reduce_context_switch:
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


def fleet_compact_reminder(_slices: AgentSlices, *, catalog: ModelCatalog) -> str:
    del catalog
    return (
        "If you are coordinating work, reconcile changed task status and record "
        "outstanding assignments, promised reports, blockers and artifact locations "
        "in your durable working notes. Keep any user-facing commitments current "
        "in the shared to-user document. Update existing records only when something "
        "changed; compaction alone is not a reason to send peers or the user a message."
    )


def contribute() -> PluginContributions:
    return PluginContributions(
        system_prompt_sections=(fleet_self_section, reduce_context_switch_section),
        compact_reminder_sections=(fleet_compact_reminder,),
    )
