"""Bootstrap contract for a coding process launched to replace its Ava owner."""

import shlex
import sys
from pathlib import Path


def bootstrap_message(
    agent_id: int,
    name: str,
    provider: str,
    brief: str,
    guide: Path,
) -> str:
    """Name both identities explicitly and inline the briefing in the launch message."""
    if provider not in ("codex", "claude") or not name.strip():
        raise ValueError("A takeover needs a session name and codex/claude provider")
    command = shlex.join(
        [
            sys.executable,
            "-m",
            "cli",
            "impersonate",
            "request",
            "--agent",
            str(agent_id),
            "--name",
            name,
            "--as",
            f"{provider.title()}: {name}",
            "--provider",
            provider,
            "--reason",
            "Take over the launching Ava agent. The briefing is in the launch message.",
        ]
    )
    routing = (
        "Add --thread-id with this Codex session's CODEX_THREAD_ID; preserve CODEX_HOME "
        "when issuing the request so the native relay uses this session's owner."
        if provider == "codex"
        else "Immediately start the Claude Monitor relay with --session <returned id> and the "
        "returned relay credential, as described in the guide."
    )
    return (
        f"You will take over Ava agent {agent_id}, the agent that launched you. "
        f"The briefing:\n{brief}\n\n"
        f"Read the impersonator guide at {guide}. "
        f"Start the named impersonation with: {command}\n{routing}\n"
        "No controller credential is issued: control commands (say/inbox/ack/renew/release) run "
        "under this session's id and are pinned to this process tree. Wait for status active "
        "before acting as this agent. Its execution pauses automatically when the session starts. "
        "Use ava impersonate say for all "
        "user-facing progress and questions; process and ACK inbound messages from the relay. "
        "When complete, flush attached SDK work, stop your work, then release with your own "
        "summary. Ava generates impersonation/<id>.json and delivers it with that summary "
        "as the first resumed system note. After release, stop."
    )
