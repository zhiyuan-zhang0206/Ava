"""Opt-in external tool identity inherited once by its subprocess tree.

AVA_CALLER_IDENTITY is asserted provenance, not credentials. It declares an
external agent and never stands for the human, system, or an Ava agent whose
environment was inherited.
"""

from __future__ import annotations

import os
import shlex

from base.agents.messages.caller_identity import CallerIdentity


def external_caller() -> CallerIdentity | None:
    """Read a strictly validated explicit external profile, or no opt-in."""
    # env-ok: external subprocess provenance handoff, not cluster configuration
    raw = os.environ.get("AVA_CALLER_IDENTITY")
    if raw is None:
        return None
    return CallerIdentity.model_validate_json(raw)


def launch_caller_assignment(tool: str, instance: str | None) -> str:
    """Optional shell-safe launch assignment; no opt-in means no activation."""
    if instance is None:
        return ""
    caller = CallerIdentity(kind="external_agent", subject=tool, instance=instance)
    return f"AVA_CALLER_IDENTITY={shlex.quote(caller.model_dump_json(exclude_none=True))} "
