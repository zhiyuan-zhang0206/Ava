"""Shell-TTL deadline merge shared by the gateway's shell read paths.

The TTL mandate (user rulings 2026-08-27 / 2026-09-01) makes every
persistent shell session carry a hard lifetime: `agent_shell_ttls` rows
record it, the gateway TTL reaper enforces it, and every read path must
answer a deadline. A session without a row — a legacy pre-mandate shell, or
one created by a not-yet-updated runner process during a rollout — falls
back to the 24h cap counted from its launch epoch.
"""

from datetime import datetime, timedelta

# The session hard cap (user ruling 2026-09-01). Twin of
# ava.shell.sessions._MAX_TTL_SECONDS — the gateway cannot import the SDK
# layer, so the value is duplicated deliberately.
MAX_SHELL_TTL_SECONDS = 86_400


def fallback_expiry(created_at: datetime | None) -> datetime | None:
    """The deadline for a session with no TTL row: launch epoch + 24h cap.

    None only when the launch epoch is unknown — with nothing to count from,
    the caller keeps its defensive no-TTL rendering."""
    if created_at is None:
        return None
    return created_at + timedelta(seconds=MAX_SHELL_TTL_SECONDS)
