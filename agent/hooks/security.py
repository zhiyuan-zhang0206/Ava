"""Delivery of the prompt-injection findings an exec child raised.

`ava.security.scan_content` runs inside the exec child, which has no messages to write to: it
appends each finding to the turn's state update (`ava.state_update["security_findings"]`), the
exec node commits the delta to `state.security_findings`, and this after_exec hook turns the
entries into SECURITY system notes and resets the channel. The note lands right behind the
exec-result ToolMessage(s) — the hook runs once every tool result of the AIMessage has committed,
and the tool_use -> tool_result adjacency invariant forbids a note between them.

The findings are checkpointed, so one that was committed and not yet delivered when the process
died is delivered after the restart. The agent host keeps no findings of its own: it only ever
reads the graph state it is handed.
"""

from __future__ import annotations

from datetime import UTC, datetime

from langchain_core.runnables import RunnableConfig
from langgraph.runtime import Runtime
from langgraph.types import Overwrite

from agent import state as _state
from agent.hooks._registry import Hook
from agent.messages import security_note_message
from base.agents.context import AvaContext
from base.config import settings
from base.log import logger


class _DeliverSecurityFindingsHook(Hook):
    """after_exec: one SECURITY note per pending finding, then clear them.

    No-op (returns None) when no finding is pending. Findings are cleared even when scanning has
    since been switched off, so a stale entry never outlives the setting.
    """

    async def __call__(
        self,
        state: _state.AgentState,
        _runtime: Runtime[AvaContext],
        _config: RunnableConfig,
        /,
    ) -> dict | None:
        findings = state.security_findings
        if not findings:
            return None
        if not settings.agent.security_scan_enabled:
            logger.warning(
                "dropping {} pending prompt-injection finding(s): security scanning is disabled",
                len(findings),
            )
            return {"security_findings": Overwrite([])}
        now = datetime.now(UTC)
        return {
            "messages": [
                security_note_message(source=f.source, triggers=f.triggers, created_at=now)
                for f in findings
            ],
            "security_findings": Overwrite([]),
        }


# Module-level singleton — the instance `framework_hooks()` hands the graph build.
_deliver_security_findings = _DeliverSecurityFindingsHook()
