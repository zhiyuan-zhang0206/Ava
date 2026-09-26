"""Bootstrap contract for a coding process launched to replace its Ava owner."""

import shlex
import sys
from pathlib import Path

# The generated request command spells out both former defaults because the CLI
# takes neither as an implicit default (user ruling 2026-09-20, task #4102).
# --ttl: the lease's recovery deadline — one hour was the request's effective
# value before the ruling; the executor extends it deliberately from renewal
# reminders, never on a timer (impersonator guide). --batch-window 0: deliver
# immediately — no coalescing of routine arrivals, the former effective value.
_TAKEOVER_TTL_SECONDS = 3600
_TAKEOVER_BATCH_WINDOW_SECONDS = 0


def bootstrap_message(
    agent_id: int,
    name: str,
    provider: str,
    brief: str,
    guide: Path,
    codex_remote: str | None = None,
    *,
    relay_resident: bool = False,
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
            "--ttl",
            str(_TAKEOVER_TTL_SECONDS),
            "--batch-window",
            str(_TAKEOVER_BATCH_WINDOW_SECONDS),
            "--reason",
            "Take over the launching Ava agent. The briefing is in the launch message.",
        ]
    )
    codex_routing = (
        "Add --thread-id with this Codex session's CODEX_THREAD_ID and --codex-remote "
        f"{codex_remote} when issuing the request; preserve CODEX_HOME so the native relay "
        "delivers into that same server via Steer — the TUI and the "
        "relay must share one endpoint."
        if codex_remote is not None
        else "Add --thread-id with this Codex session's CODEX_THREAD_ID; preserve CODEX_HOME "
        "when issuing the request so the native relay finds this session's server. When your host "
        "runs an explicit app server, pass --codex-remote with its endpoint — the TUI and the "
        "relay must share one endpoint."
    )
    if provider == "codex":
        routing = codex_routing
    elif relay_resident:
        routing = (
            "Its relay is started automatically for this session by the loaded Ava relay "
            "plugin: the request output names the credential stub it writes; do not arm a "
            "Monitor watch. If the stub is not consumed and no relay heartbeat starts, "
            "fall back to the manual flow the request output describes."
        )
    else:
        routing = (
            "Immediately start the Claude Monitor relay with --session <returned id> and the "
            "returned relay credential, as the request output instructs."
        )
    return (
        f"You will take over Ava agent {agent_id}, the agent that launched you. "
        f"The briefing:\n{brief}\n\n"
        f"Read the impersonator guide at {guide}. "
        f"Start the named impersonation with: {command}\n{routing}\n"
        "No controller credential is issued: control commands (say/inbox/ack/renew/release) run "
        "under this session's id and are pinned to this process tree. Wait for status active "
        "before acting as this agent. Its execution pauses automatically when the session starts. "
        "Once active, verify the start message actually arrives in this conversation before "
        "relying on automatic delivery — transport acceptance is not host receipt. "
        "Use ava impersonate say for all "
        "user-facing progress and questions; process and ACK inbound messages from the relay. "
        "When complete, flush attached SDK work, stop your work, then release with your own "
        "summary. Ava generates impersonation/<id>.json and delivers it with that summary "
        "as the first resumed system note. After release, stop."
    )
