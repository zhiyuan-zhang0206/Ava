"""Provider transports for the shared impersonation relay process.

Adapters select the host destination, encode its input and declare who sends
terminal notices. Inbox reads, reservations, ACK windows, batching and retries
remain in the common relay loop.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from uuid import UUID

from base.agents.impersonation.host_transport import live_submit, require_control_endpoint


@dataclass(frozen=True)
class RelayAdapter:
    """A bound sender and its terminal-notice ownership.

    Codex terminal notices come from the native host's durable notice scan.
    Controller-session transports send their own notices through stdout.
    """

    send: Callable[[str], None]
    notify_terminal: bool


def emit_claude(message: str) -> None:
    """Flush text to the owning Claude Monitor."""
    print(message, flush=True)


def emit_dsh(message: str) -> None:
    """Keep each envelope in one JSON stdout line for the owning dsh plugin."""
    print(json.dumps(message, ensure_ascii=False), flush=True)


def resolve_adapter(
    provider: str, thread_id: str | None, *, codex_remote: str | None = None
) -> RelayAdapter:
    """Bind the provider's destination before the relay opens its inbox.

    Codex requires live Steer delivery; refusal or transport failure raises
    without queue fallback. Controller-session destinations reject Codex options.
    """
    if provider == "codex":
        if thread_id is None:
            raise ValueError("codex relay requires --thread-id for an existing session")
        target = UUID(thread_id)
        endpoint = require_control_endpoint(codex_remote)

        def send(message: str) -> None:
            reason = live_submit(str(target), message, endpoint=endpoint)
            if reason is not None:
                raise RuntimeError(f"Codex Steer delivery failed: {reason}")

        return RelayAdapter(send, notify_terminal=False)
    if provider in ("claude", "dsh"):
        if codex_remote is not None or thread_id is not None:
            raise ValueError(
                f"The {provider} relay routes to its owner; omit --thread-id/--codex-remote"
            )
        return RelayAdapter(emit_claude if provider == "claude" else emit_dsh, notify_terminal=True)
    raise ValueError(f"Unknown relay provider: {provider}")
