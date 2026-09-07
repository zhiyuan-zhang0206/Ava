"""`ava agents` — operator CLI for agent lifecycle (thin client over the gateway).

The from-the-box ops surface: observe and control agent processes without curling
the gateway or opening the web UI. Each verb forwards to an existing gateway route
(the gateway owns the effect; the CLI adds only rendering + arg parsing) and fails
fast (`raise_for_status()`) on any HTTP error. Ordered by escalating force:

  ls              GET  /api/agents?fields=summary        list id / status / machine / label
  send <id> <txt> POST /api/agents/{id}/messages         deliver a chat inbound (source required)
  cancel <id>     POST /api/cancel                       halt the current action -> idle, stays alive
  restart <id>    POST /api/agents/{id}/restart          bounce the process, state preserved
  terminate <id>  POST /api/agents/{id}/terminate        graceful stop + exit
  kill <id>       POST /api/agents/{id}/terminate(force) hard-stop a stuck agent

`send` is the shell-level message primitive: the completion notices of
`ava.shell.run_background` and watcher exit notices are generated command lines
ending in `ava agents send ... --source shell:N|watcher:N`, and a host operator
can message any agent directly. Richer capabilities (spawn an agent, inspect its
events) stay in the `ava.*` SDK and the web UI.
"""

from __future__ import annotations

from pydantic import BaseModel

_TIMEOUT_S = 15.0


def _caller_body(source: str | None, *, field: str = "source") -> dict[str, str]:
    """Opt-in provenance; no caller metadata is authentication evidence."""
    from shared.external_caller import explicit_caller_source

    resolved = explicit_caller_source(source)
    return {field: resolved} if resolved is not None else {}


class _AgentListItem(BaseModel):
    """The small subset rendered by ``ava agents ls``."""

    agent_id: int
    status: str
    machine: str
    label: str | None


def cmd_agents_ls() -> int:
    """List every agent's id, status, machine, and label via the summary projection.

    Terminated agents are listed too (the gateway returns the full set); the
    status column is the live lifecycle state."""
    from shared.http_dial import get as dial_get
    from shared.machine import gateway_api_base, gateway_auth_headers

    url = f"{gateway_api_base()}/api/agents?fields=summary"
    resp = dial_get(url, timeout=_TIMEOUT_S, headers=gateway_auth_headers())
    resp.raise_for_status()
    rows = [_AgentListItem.model_validate(r) for r in resp.json()]

    if not rows:
        print("(no agents)")
        return 0

    id_w = max(len("id"), *(len(str(r.agent_id)) for r in rows))
    status_w = max(len("status"), *(len(str(r.status)) for r in rows))
    machine_w = max(len("machine"), *(len(r.machine) for r in rows))
    print(f"{'id'.rjust(id_w)}  {'status'.ljust(status_w)}  {'machine'.ljust(machine_w)}  label")
    for r in rows:
        label = r.label or ""
        print(
            f"{str(r.agent_id).rjust(id_w)}  {str(r.status).ljust(status_w)}  "
            f"{r.machine.ljust(machine_w)}  {label}"
        )
    return 0


# How much of a --tail-file is appended to the message (bytes read from the
# end; decoded with errors="replace" so a mid-character cut cannot break the
# POST). Fixed — a caller who needs more reads the file itself.
_TAIL_BYTES = 2048


def cmd_agents_send(
    agent_id: int, content: str, source: str | None, tail_file: str | None = None
) -> int:
    """`ava agents send <id> <content> --source S [--tail-file PATH]` — deliver a
    chat inbound via POST /api/agents/{id}/messages.

    `--source` or AVA_CALLER_IDENTITY is required: every message must carry an honest
    provenance — machine callers pass `shell:N` / `watcher:N`, a human operator
    passes `user`. An illegal source is rejected 422 at the gateway boundary
    (`AgentMessageIn.source` -> `shared.envelope.validate_source`) and the
    response body is printed so the caller sees the legal set.

    `--tail-file` appends the last `_TAIL_BYTES` bytes of PATH to the message —
    the background-run / watcher completion notices use it to carry the end of
    the command's output (result line or traceback) so the agent usually does
    not need a follow-up read. Delivery auto-resurrects a terminated target
    (gateway behavior, same as the SDK path)."""
    import os
    import sys
    from pathlib import Path

    from shared.http_dial import post as dial_post
    from shared.machine import gateway_api_base, gateway_auth_headers

    caller = _caller_body(source)
    if "source" not in caller:
        raise ValueError("send requires --source or an explicit AVA_CALLER_IDENTITY profile")
    if tail_file is not None:
        # Delivering the notice is the primary contract; the tail is a rider.
        # An unreadable tail file must not abort the POST — the failure is
        # surfaced inside the delivered message instead, so the agent still
        # learns its command finished and sees why the tail is missing.
        try:
            with Path(tail_file).open("rb") as f:
                f.seek(0, os.SEEK_END)
                f.seek(max(0, f.tell() - _TAIL_BYTES))
                tail = f.read().decode("utf-8", errors="replace")
        except OSError as e:
            content += f"\n\n[tail unavailable: {e}]"
        else:
            if tail.strip():
                content += f"\n\nLast output ({tail_file}):\n{tail.strip()}"
    url = f"{gateway_api_base()}/api/agents/{agent_id}/messages"
    resp = dial_post(
        url,
        json={"content": content, **caller},
        timeout=_TIMEOUT_S,
        headers=gateway_auth_headers(),
    )
    if resp.status_code >= 400:
        # Surface the response body before raising: the 422 detail carries the
        # legal source set / validation reason, which is the actionable part.
        print(resp.text, file=sys.stderr)
    resp.raise_for_status()
    print(f"  ✓ agent {agent_id} send: {resp.json().get('status')}")
    return 0


def cmd_agents_cancel(agent_id: int) -> int:
    """`ava agents cancel <id>` — halt the current action via POST /api/cancel.

    A running step interrupts immediately; if the agent is between steps the next
    claim halts it to idle. Either way it stops but stays alive and resumes on the
    next message — the soft stop, vs terminate / kill which end the agent."""
    from shared.http_dial import post as dial_post
    from shared.machine import gateway_api_base, gateway_auth_headers

    url = f"{gateway_api_base()}/api/cancel"
    resp = dial_post(
        url, json={"agent_id": agent_id}, timeout=_TIMEOUT_S, headers=gateway_auth_headers()
    )
    resp.raise_for_status()
    print(f"  ✓ agent {agent_id} cancel: {resp.json().get('status')}")
    return 0


def cmd_agents_restart(
    agent_id: int,
    config_json: str | None = None,
    *,
    source: str | None = None,
) -> int:
    """`ava agents restart <id> [--config JSON]` — POST /api/agents/{id}/restart.

    The agent exits after its current turn and a fresh process is respawned
    attached to the same agent_id (history preserved). `--config` merges a
    per-agent overlay before the restart. A dead agent returns
    `already_terminated` — use `resurrect` instead."""
    import json
    import sys

    from shared.http_dial import post as dial_post
    from shared.machine import gateway_api_base, gateway_auth_headers

    url = f"{gateway_api_base()}/api/agents/{agent_id}/restart"
    caller = _caller_body(source)
    if config_json is None:
        resp = dial_post(
            url,
            **({"json": caller} if caller else {}),
            timeout=_TIMEOUT_S,
            headers=gateway_auth_headers(),
        )
    else:
        try:
            config_overlay = json.loads(config_json)
        except json.JSONDecodeError as exc:
            print(f"invalid config JSON: {exc}", file=sys.stderr)
            return 1
        if not isinstance(config_overlay, dict):
            print("config must be a JSON object", file=sys.stderr)
            return 1
        resp = dial_post(
            url,
            json={"config_overlay": config_overlay, **caller},
            timeout=_TIMEOUT_S,
            headers=gateway_auth_headers(),
        )
    resp.raise_for_status()
    print(f"  ✓ agent {agent_id} restart: {resp.json().get('status')}")
    return 0


def cmd_agents_resurrect(agent_id: int, *, source: str | None = None) -> int:
    """`ava agents resurrect <id>` — POST /api/agents/{id}/resurrect.

    Brings a terminated agent back: a fresh process is respawned attached to the
    same agent_id (history preserved). An already-running agent returns
    `already_alive`."""
    from shared.http_dial import post as dial_post
    from shared.machine import gateway_api_base, gateway_auth_headers

    url = f"{gateway_api_base()}/api/agents/{agent_id}/resurrect"
    caller = _caller_body(source, field="resurrected_by")
    resp = dial_post(
        url,
        **({"json": caller} if caller else {}),
        timeout=_TIMEOUT_S,
        headers=gateway_auth_headers(),
    )
    resp.raise_for_status()
    print(f"  ✓ agent {agent_id} resurrect: {resp.json().get('status')}")
    return 0


def _terminate(agent_id: int, *, force: bool, source: str | None = None) -> int:
    """Shared POST for `terminate` (graceful) and `kill` (force) — both hit
    POST /api/agents/{id}/terminate, differing only in the `force` flag. The
    An explicit source/profile is forwarded unchanged. Non-opted-in legacy
    callers retain the old server default until the negotiated transition."""
    from shared.http_dial import post as dial_post
    from shared.machine import gateway_api_base, gateway_auth_headers

    verb = "kill" if force else "terminate"
    url = f"{gateway_api_base()}/api/agents/{agent_id}/terminate"
    resp = dial_post(
        url,
        json={"force": force, **_caller_body(source)},
        timeout=_TIMEOUT_S,
        headers=gateway_auth_headers(),
    )
    resp.raise_for_status()
    print(f"  ✓ agent {agent_id} {verb}: {resp.json().get('status')}")
    return 0


def cmd_agents_terminate(agent_id: int, *, source: str | None = None) -> int:
    """`ava agents terminate <id>` — graceful stop: the agent exits after
    processing its current turn. For an agent wedged mid-turn (a hung step) that
    cannot reach the graceful exit, use `kill`."""
    return _terminate(agent_id, force=False, source=source)


def cmd_agents_kill(agent_id: int, *, source: str | None = None) -> int:
    """`ava agents kill <id>` — request forceful interruption. Hosted work may
    return enqueued while it drains; this is acceptance, not observed exit.
    The response acknowledges the host lifecycle request; completion is asynchronous."""
    return _terminate(agent_id, force=True, source=source)
