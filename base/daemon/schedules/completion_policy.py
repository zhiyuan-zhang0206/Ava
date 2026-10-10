"""Completion-notice policy vocabulary shared by config and delivery readers."""

from enum import StrEnum


class CompletionNoticePolicy(StrEnum):
    """How admitted platform completions reach an agent."""

    ALL = "all"
    HOURLY = "hourly"


def validate_completion_notice_policy(value: str) -> CompletionNoticePolicy:
    """Return a supported policy or fail loudly on a stored invalid value."""
    try:
        return CompletionNoticePolicy(value)
    except ValueError as exc:
        raise ValueError(
            f"completion_notice_policy must be one of "
            f"{[policy.value for policy in CompletionNoticePolicy]!r}, got {value!r}"
        ) from exc
