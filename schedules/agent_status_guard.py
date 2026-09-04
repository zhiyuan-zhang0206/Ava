"""Startup guard for schedule dependencies on ``AgentStatus`` members."""

from __future__ import annotations


def ensure_agent_status_members(
    agent_status: type[object],
    required_members: set[str],
    *,
    schedule_name: str,
) -> None:
    """Exit before schedule work starts when a required enum member is absent."""
    missing: list[str] = []
    for member_name in sorted(required_members):
        try:
            getattr(agent_status, member_name)
        except AttributeError:
            missing.append(member_name)
    if missing:
        raise SystemExit(
            f"schedule {schedule_name!r} is missing AgentStatus members: {', '.join(missing)}"
        )
