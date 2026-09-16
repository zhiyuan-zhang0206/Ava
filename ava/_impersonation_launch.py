"""Bootstrap contract for a coding process launched to replace its Ava owner."""

import shlex
import sys
from pathlib import Path


def bootstrap_message(
    agent_id: int,
    name: str,
    provider: str,
    tasks_file: Path,
    work_file: Path,
    guide: Path,
) -> str:
    """Name both identities explicitly before the controller requests a takeover."""
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
            f"Take over the launching Ava agent. Read the handoff brief at {tasks_file}.",
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
        f"Read its handoff brief at {tasks_file}, then the impersonator guide at {guide}. "
        f"Start the named impersonation with: {command}\n{routing}\n"
        "No controller credential is issued: control commands (say/inbox/ack/renew/release) run "
        "under this session's id and are pinned to this process tree. Wait for status active "
        "before acting as this agent. Its native loop pauses automatically after a durable "
        "checkpoint. "
        "Use ava impersonate say for all "
        "user-facing progress and questions; process and ACK inbound messages from the relay. "
        f"Keep {work_file} updated with STATUS: WORKING and a work log for process supervision. "
        "When complete, flush attached SDK work, stop your work, then release with your own "
        "summary. Ava generates impersonation/<id>.json and delivers it with that summary "
        "as the first resumed system note. After release, set STATUS: DONE and stop."
    )
