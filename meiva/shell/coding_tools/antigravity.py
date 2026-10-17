"""Launch, message, inspect, or stop Google Antigravity conversations.

Unlike terminal coding tools (Claude Code, Codex), Antigravity conversations
run inside the local Antigravity ``language_server`` and are controlled via its
Connect JSON-RPC endpoint (``StartCascade``, ``SendAgentMessage``,
``GetConversationMetadata``, ``ForceStopCascadeTree``).

A supervised worker (the default) creates ``tasks.md`` and ``work.md`` and
starts a new Antigravity conversation with the two-file collaboration contract,
ready to be monitored with ``watch_work.py``. An interactive prompt launch
(``prompt``) starts a new conversation with that initial prompt and no task/work
files. Follow-up messages (including instructions to impersonate an Meiva agent)
are delivered via ``send(conversation_id, message)``.
"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import UUID

from base.agents.impersonation.host_transport import antigravity_rpc

from ._common import init_file, worker_bootstrap
from ._common import resolve_dir as resolve_dir
from ._common import resolve_file as resolve_file
from ._common import session_uuid as session_uuid


def launch(
    workspace: Path,
    tasks_file: Path | None,
    work_file: Path | None,
    *,
    skill_dir: Path,
    prompt: str | None = None,
) -> int:
    """Start a new Antigravity conversation on ``workspace``.

    When ``prompt`` is given, starts the conversation with that prompt and no
    task/work files. Otherwise initializes ``tasks_file`` and ``work_file`` and
    starts the conversation with the supervised collaboration contract.
    """
    if prompt is not None:
        if not prompt.strip():
            raise ValueError("a prompt launch needs a non-empty prompt")
        if tasks_file is not None or work_file is not None:
            raise ValueError("a prompt launch does not use task or work files")
        initial_prompt = prompt
    else:
        if tasks_file is None or work_file is None:
            raise ValueError("a supervised launch needs its task and work files")
        init_file(tasks_file, "")
        init_file(work_file, "STATUS: WORKING\n\n## Log\n\n")
        initial_prompt = worker_bootstrap(
            skill_dir / "references" / "collaboration_protocol.md",
            workspace,
            tasks_file,
            work_file,
        )

    response = antigravity_rpc(
        "StartCascade",
        {
            "initialPrompt": initial_prompt,
            "workspaceUris": [workspace.as_uri()],
        },
    )
    cascade_id = str(response.get("cascadeId") or "").strip()
    if not cascade_id:
        raise RuntimeError(f"Antigravity StartCascade returned no cascadeId: {response!r}")

    print(f"ready. workspace={workspace}")
    if tasks_file is not None and work_file is not None:
        print(f"tasks_file={tasks_file}")
        print(f"work_file={work_file}")
    print(f"antigravity_conversation={cascade_id}")
    return 0


def send(conversation_id: str, message: str) -> int:
    """Send a follow-up message to an existing Antigravity conversation."""
    target = str(UUID(conversation_id))
    if not message.strip():
        raise ValueError("message must be non-empty")
    antigravity_rpc(
        "SendAgentMessage",
        {"recipient": target, "content": message},
    )
    print(f"sent. antigravity_conversation={target}")
    return 0


def status(conversation_id: str) -> int:
    """Query metadata for an Antigravity conversation."""
    target = str(UUID(conversation_id))
    response = antigravity_rpc(
        "GetConversationMetadata",
        {"conversationId": target},
    )
    print("status=ok")
    print(f"antigravity_conversation={target}")
    print(f"metadata={json.dumps(response, ensure_ascii=False)}")
    return 0


def stop(conversation_id: str) -> int:
    """Force-stop an Antigravity conversation tree."""
    target = str(UUID(conversation_id))
    response = antigravity_rpc(
        "ForceStopCascadeTree",
        {"conversationId": target},
    )
    stopped = response.get("stoppedConversationIds") or [target]
    print(f"stopped. antigravity_conversation={target}")
    print(f"stopped_ids={json.dumps(stopped, ensure_ascii=False)}")
    return 0
