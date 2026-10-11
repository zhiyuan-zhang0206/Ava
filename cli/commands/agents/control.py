"""Agent control CLI: ordinary send, terminate, cancel, restart and resurrection."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from pydantic import BaseModel

from base.agents import ShellSessionKillTiming

_TIMEOUT_S = 15.0


class ProvenanceError(ValueError):
    """A provenance requirement failed; the CLI reports it instead of a traceback.

    Raised where an explicit ``--source`` value is missing on a requiring verb
    or is malformed. Per-command CLI handlers catch it,
    print the message, and exit 2; the command layer itself keeps raising so
    callers and tests see one honest contract.
    """


def _validated_source_arg(source: str) -> str:
    """Validate one explicit source value (the CLI parse layer runs first)."""
    from base.agents.messages.envelope import validate_source

    try:
        validate_source(source)
    except ValueError as exc:
        raise ProvenanceError(str(exc)) from exc
    return source


def _explicit_caller(source: str | None, *, field: str = "source") -> dict[str, str]:
    """Explicit provenance only — the environment never supplies or vetoes it.

    User ruling 2026-09-20: CLI parameters are explicit; the opt-in
    AVA_CALLER_IDENTITY profile (still consumed by SDK-side stamping, see
    ``ava.sdk_surface.agent_identity.require_actor``) no longer compensates an omitted ``--source``.
    """
    if source is None:
        return {}
    return {field: _validated_source_arg(source)}


class _AgentListItem(BaseModel):
    """The small subset rendered by ``ava agents ls``."""

    agent_id: int
    status: str
    machine: str
    label: str | None


def cmd_agents_ls(
    *, scope: str = "live", query: str = "", before_id: int | None = None, limit: int = 100
) -> int:
    """Render one agent directory page and its continuation cursor."""
    from base.cluster.machine import gateway_api_base, gateway_auth_headers
    from base.host.net.http_dial import get as dial_get

    url = f"{gateway_api_base()}/api/agents"
    params: dict[str, str | int] = {"scope": scope, "query": query, "limit": limit}
    if before_id is not None:
        params["before_id"] = before_id
    resp = dial_get(url, params=params, timeout=_TIMEOUT_S, headers=gateway_auth_headers())
    resp.raise_for_status()
    page = resp.json()
    rows = [_AgentListItem.model_validate(r) for r in page["agents"]]

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
    if page["next_cursor"] is not None:
        print(f"(more agents: repeat with --before-id {page['next_cursor']})")
    return 0


# How much of a --tail-file is appended to the message: its last _TAIL_LINES
# lines, read from at most the last _TAIL_BYTES bytes (decoded with
# errors="replace" so a mid-character cut cannot break the POST). Fixed — a
# caller who needs more reads the file itself.
_TAIL_LINES = 3
_TAIL_BYTES = 2048


_SEND_SOURCE_GUIDE = (
    "send requires --source (explicit provenance); nothing was sent.\n"
    "  --source user                  send as the user (the human operator)\n"
    "  --source external_agent:NAME[:INSTANCE]\n"
    "                                 send as the external agent NAME (e.g. claude_code),\n"
    "                                 distinct from the user (self-asserted, not authenticated)\n"
    "  --source shell:N | watcher:N   a machine notice (N = the producing session id)\n"
    "  --source schedule:N            a gateway schedule\n"
    "See `ava agents send --help` for the full source set."
)


def _explicit_send_source(source: str | None) -> str:
    """The send path's provenance: explicit only, never environment-compensated.

    The CLI enforces `--source` at the argparse layer; this guard covers
    programmatic callers — no path may send without an explicit, valid source
    (user ruling 2026-09-20: explicit parameters only).
    """
    if source is None:
        raise ProvenanceError(_SEND_SOURCE_GUIDE)
    return _validated_source_arg(source)


def cmd_agents_send(
    agent_id: int,
    content: str,
    source: str | None,
    tail_file: str | None = None,
    *,
    completion: bool = False,
) -> int:
    """Send chat with explicit provenance, optional output tail and finite retries.

    A call retries transient transport and known transient HTTP responses with one
    idempotency key. Final errors are exposed. Normal delivery can wake a terminated
    target on its home machine. No sender journal or later background delivery is
    created."""
    # argparse enforces --source for the CLI; the guard covers programmatic callers.
    source = _explicit_send_source(source)
    status = send_agent_message(
        agent_id,
        content,
        source=source,
        tail_file=tail_file,
        completion=completion,
    )
    print(f"  ✓ agent {agent_id} send: {status}")
    return 0


def _with_tail(content: str, tail_file: str) -> str:
    """`content` with the last `_TAIL_LINES` lines of `tail_file` appended (or why it is unavailable)."""
    import os
    from pathlib import Path

    # Delivering the notice is the primary contract; the tail is a rider.
    # An unreadable tail file must not abort the POST — the failure is
    # surfaced inside the delivered message instead, so the agent still
    # learns its command finished and sees why the tail is missing.
    try:
        with Path(tail_file).open("rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - _TAIL_BYTES))
            tail = "\n".join(f.read().decode("utf-8", errors="replace").splitlines()[-_TAIL_LINES:])
    except OSError as e:
        return content + f"\n\n[tail unavailable: {e}]"
    if tail.strip():
        return content + f"\n\nLast output ({tail_file}):\n{tail.strip()}"
    return content


def send_agent_message(
    agent_id: int,
    content: str,
    *,
    source: str,
    tail_file: str | None = None,
    completion: bool = False,
) -> str:
    """Deliver with bounded same-key retries and expose the final failure."""
    import sys
    import time
    from uuid import uuid4

    from base.agents.messages.delivery.retry import network_errors, retryable_response
    from base.cluster.machine import gateway_api_base, gateway_auth_headers
    from base.host.net.http_dial import post as dial_post

    if tail_file is not None:
        content = _with_tail(content, tail_file)
    headers = {**gateway_auth_headers(), "Idempotency-Key": str(uuid4())}
    body = {
        "content": content,
        "source": source,
        **({"completion_notice": True} if completion else {}),
    }
    url = f"{gateway_api_base()}/api/agents/{agent_id}/messages"
    for attempt in range(3):
        try:
            response = dial_post(url, json=body, timeout=_TIMEOUT_S, headers=headers)
        except network_errors():
            if attempt == 2:
                raise
        else:
            if not retryable_response(response) or attempt == 2:
                if response.status_code >= 400:
                    print(response.text, file=sys.stderr)
                response.raise_for_status()
                return str(response.json().get("status"))
        time.sleep(0.5 * (2**attempt))
    raise AssertionError("bounded message delivery exhausted without an outcome")


def cmd_agents_cancel(agent_id: int) -> int:
    """Cancel the exact observed ACTIVE native work; acceptance precedes its stop."""
    from base.agents.incarnation.native_work_models import NativeCancelAcceptance, NativeWorkTarget
    from base.cluster.machine import gateway_api_base, gateway_auth_headers
    from base.host.net.http_dial import get as dial_get
    from base.host.net.http_dial import post as dial_post

    root = f"{gateway_api_base()}/api/keyed/v1/agents/{agent_id}"
    observed = dial_get(f"{root}/native-work", timeout=_TIMEOUT_S, headers=gateway_auth_headers())
    observed.raise_for_status()
    target = NativeWorkTarget.model_validate(observed.json())
    if target.agent_id != agent_id:
        raise ValueError("work observation targets another agent")
    resp = dial_post(
        f"{root}/cancel-work",
        json=target.model_dump(mode="json"),
        timeout=_TIMEOUT_S,
        headers={
            **gateway_auth_headers(),
            "Idempotency-Key": uuid4().hex,
            "Idempotency-Scope": "principal-v1",
        },
    )
    resp.raise_for_status()
    accepted = NativeCancelAcceptance.model_validate(resp.json())
    if accepted.target != target:
        raise ValueError("cancel acceptance targets another work")
    print(f"  ✓ agent {agent_id} cancel accepted: {accepted.command_id}")
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

    from base.cluster.machine import gateway_api_base, gateway_auth_headers
    from base.host.net.http_dial import post as dial_post

    url = f"{gateway_api_base()}/api/agents/{agent_id}/restart"
    caller = _explicit_caller(source)
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
    from base.cluster.machine import gateway_api_base, gateway_auth_headers
    from base.host.net.http_dial import post as dial_post

    url = f"{gateway_api_base()}/api/agents/{agent_id}/resurrect"
    caller = _explicit_caller(source, field="resurrected_by")
    resp = dial_post(
        url,
        **({"json": caller} if caller else {}),
        timeout=_TIMEOUT_S,
        headers=gateway_auth_headers(),
    )
    resp.raise_for_status()
    print(f"  ✓ agent {agent_id} resurrect: {resp.json().get('status')}")
    return 0


def _terminate(
    agent_id: int,
    *,
    force: bool,
    source: str | None = None,
    kill_all_shell_sessions: bool = False,
) -> int:
    """Shared POST for `terminate` (graceful) and `kill` (force) — both hit
    POST /api/agents/{id}/terminate, differing only in the `force` flag. An
    explicit source is forwarded unchanged; the environment is never consulted
    (user ruling 2026-09-20). Omitting it claims no provenance and
    leaves the server default in place.
    `kill_all_shell_sessions` is sent only when set; the output reports what
    the kill did, so it is verifiable from the output alone."""
    from base.cluster.machine import gateway_api_base, gateway_auth_headers
    from base.host.net.http_dial import post as dial_post

    verb = "kill" if force else "terminate"
    url = f"{gateway_api_base()}/api/agents/{agent_id}/terminate"
    kill_flag = {"kill_all_shell_sessions": True} if kill_all_shell_sessions else {}
    resp = dial_post(
        url,
        json={"force": force, **kill_flag, **_explicit_caller(source)},
        timeout=_TIMEOUT_S,
        headers=gateway_auth_headers(),
    )
    resp.raise_for_status()
    data = resp.json()
    suffix = _shell_sessions_suffix(data.get("shell_sessions"), requested=kill_all_shell_sessions)
    print(f"  ✓ agent {agent_id} {verb}: {data.get('status')}{suffix}")
    return 0


def _shell_sessions_suffix(shell: dict[str, Any] | None, *, requested: bool) -> str:
    """Render the response's shell-session kill report for the status line.

    A requested kill answered without a report came from a version that does
    not know the option: nothing was killed, and the output says so."""
    if shell is None:
        return " — shell sessions NOT killed (its runner predates the option)" if requested else ""
    when = ShellSessionKillTiming(shell["when"])
    if when is ShellSessionKillTiming.AT_EXIT:
        return " — its shell sessions are killed when it exits"
    killed: list[int] = shell["killed"]
    if not killed:
        return " — no shell sessions to kill"
    return f" — killed {len(killed)} shell session(s): {', '.join(str(i) for i in killed)}"


def cmd_agents_terminate(
    agent_id: int, *, source: str | None = None, kill_all_shell_sessions: bool = False
) -> int:
    """`ava agents terminate <id>` — graceful stop: the agent exits after
    processing its current turn. For an agent wedged mid-turn (a hung step) that
    cannot reach the graceful exit, use `kill`. A terminated agent is resurrected
    by any new message, including its own shells' and watchers' messages. With
    `--kill-all-shell-sessions` every shell session it owns on its home machine
    is killed too, silently: right before the termination applies (after the
    agent's last step), or right away when it is already terminated."""
    return _terminate(
        agent_id, force=False, source=source, kill_all_shell_sessions=kill_all_shell_sessions
    )


def cmd_agents_kill(
    agent_id: int, *, source: str | None = None, kill_all_shell_sessions: bool = False
) -> int:
    """`ava agents kill <id>` — request forceful interruption. Hosted work may
    return enqueued while it drains; this is acceptance, not observed exit.
    The response acknowledges the host lifecycle request; completion is asynchronous.
    `--kill-all-shell-sessions` also kills every shell session the agent owns on
    its home machine, right away (see `terminate`). A kill supersedes an earlier
    graceful terminate's request, so pass the option here too if still wanted."""
    return _terminate(
        agent_id, force=True, source=source, kill_all_shell_sessions=kill_all_shell_sessions
    )


def cmd_agents_compact(agent_id: int) -> int:
    """Accept manual compaction of the exact observed closed source history."""
    from base.agents.compaction.models import CompactAcceptance, CompactTarget
    from base.cluster.machine import gateway_api_base, gateway_auth_headers
    from base.host.net.http_dial import get as dial_get
    from base.host.net.http_dial import post as dial_post

    root = f"{gateway_api_base()}/api/keyed/v1/agents/{agent_id}"
    observed = dial_get(
        f"{root}/compact-target", timeout=_TIMEOUT_S, headers=gateway_auth_headers()
    )
    observed.raise_for_status()
    target = CompactTarget.model_validate(observed.json())
    if target.source.agent_id != agent_id:
        raise ValueError("compact observation targets another agent")
    resp = dial_post(
        f"{root}/compact-history",
        json=target.model_dump(mode="json"),
        timeout=_TIMEOUT_S,
        headers={
            **gateway_auth_headers(),
            "Idempotency-Key": uuid4().hex,
            "Idempotency-Scope": "principal-v1",
        },
    )
    resp.raise_for_status()
    accepted = CompactAcceptance.model_validate(resp.json())
    if accepted.target != target:
        raise ValueError("compact acceptance targets another history")
    print(f"  ✓ agent {agent_id} compact accepted: {accepted.command_id}")
    return 0
