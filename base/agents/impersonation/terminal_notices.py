"""Ended-lease notice text and its single best-effort delivery to a Codex thread.

The host submits the notice once, when it observes the lease already ended. Host
acceptance is the only success this transport proves. Nothing is recorded and
nothing retries: a notice that is not accepted is reported by the caller and lost.
"""

from typing import Any

from base.agents.impersonation import host_transport
from base.db import Database


def notice_text(snapshot: dict[str, Any]) -> str:
    """Describe exactly the ended historical lease, never a replacement's authority."""
    native = (
        "The agent was terminated; no active native runtime is implied. "
        if snapshot["reason"] == "terminated: agent was terminated"
        else "Native authority is restored; its durable handoff may still be restoring. "
    )
    return (
        f"Meiva impersonation lease {snapshot['session_id']} ({snapshot['lease_id']}) "
        f"for agent {snapshot['agent_id']} ended: {snapshot['status']}. "
        f"Reason: {snapshot['reason'] or snapshot['status']}. Ended at: {snapshot['ended_at']}. "
        "That lease no longer grants external control. " + native + "This notice concerns only "
        "the named lease and does not end or cancel any newer takeover or its work. "
        f"Notice ID: impersonation-ended:{snapshot['lease_id']}."
    )


def send_ended_notice(db: Database, lease_id: str) -> str | None:
    """Submit the ended lease's notice to its recorded Codex or Antigravity thread, once.

    Returns None when the notice was accepted or the lease has no host IPC
    destination (controller-session transports print their own notices), else a
    short failure reason. The submission has its own five-second deadline.
    """
    with db.connect() as conn:
        row = conn.execute(
            "SELECT terminal_notice_snapshot FROM agent_impersonations WHERE id=%s", (lease_id,)
        ).fetchone()
    snapshot = None if row is None else row[0]
    if not snapshot:
        return None
    if snapshot["provider"] == "antigravity":
        thread_id = snapshot["thread_id"]
        if not thread_id:
            return "the recorded Antigravity destination is not deliverable"
        return host_transport.antigravity_live_submit(thread_id, notice_text(snapshot))
    if snapshot["provider"] != "codex":
        return None
    endpoint, thread_id = snapshot["endpoint"], snapshot["thread_id"]
    if (
        not endpoint
        or not thread_id
        or endpoint == "unix://"
        or not endpoint.startswith(("unix://", "ws://", "wss://"))
    ):
        return "the recorded Codex destination is not deliverable"
    return host_transport.live_submit(thread_id, notice_text(snapshot), endpoint=endpoint)
