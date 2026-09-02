"""Opt-in external tool identity inherited once by its subprocess tree.

AVA_CALLER_IDENTITY is asserted provenance, not credentials. It must never
impersonate the human, system, or an Ava agent whose environment was inherited.
"""

from __future__ import annotations

import os
import shlex

from shared.caller_identity import CallerIdentity
from shared.envelope import validate_source


def external_caller() -> CallerIdentity | None:
    """Read a strictly validated explicit external profile, or no opt-in."""
    # env-ok: external subprocess provenance handoff, not cluster configuration
    raw = os.environ.get("AVA_CALLER_IDENTITY")
    if raw is None:
        return None
    caller = CallerIdentity.model_validate_json(raw)
    if caller.kind != "external_agent":
        raise ValueError("AVA_CALLER_IDENTITY must identify an external_agent")
    return caller


def explicit_caller_source(source: str | None = None) -> str | None:
    """Resolve profile/source consistently without inferring a human principal.

    None preserves a legacy non-opted-in call site; callers that require an
    explicit source must reject it. An external profile cannot be overridden
    with user/agent/system, even when a CLI flag supplies that conflicting label.
    """
    caller = external_caller()
    if caller is not None:
        projected = caller.source()
        if source is not None and source != projected:
            raise ValueError("explicit source conflicts with AVA_CALLER_IDENTITY")
        return projected
    if source is not None:
        validate_source(source)
    return source


def launch_caller_assignment(tool: str, instance: str | None) -> str:
    """Optional shell-safe launch assignment; no opt-in means no activation."""
    if instance is None:
        return ""
    caller = CallerIdentity(kind="external_agent", subject=tool, instance=instance)
    return f"AVA_CALLER_IDENTITY={shlex.quote(caller.model_dump_json(exclude_none=True))} "
