"""`ava agents` — operator CLI for agent lifecycle (thin client over the gateway).

The from-the-box ops surface: observe and control agent processes without curling
the gateway or opening the web UI. Each verb forwards to an existing gateway route
(the gateway owns the effect; the CLI adds only rendering + arg parsing) and fails
fast (`raise_for_status()`) on any HTTP error. Ordered by escalating force:

  ls              GET  /api/agents                       read one directory page
  send <id> <txt> POST /api/agents/{id}/messages         deliver a chat inbound (source required)
  cancel <id>     POST /api/cancel                       halt the current action -> idle, stays alive
  compact <id>    POST /api/agents/{id}/compact          request conversation compaction (durable)
  restart <id>    POST /api/agents/{id}/restart          bounce the process, state preserved
  terminate <id>  POST /api/agents/{id}/terminate        graceful stop + exit
  kill <id>       POST /api/agents/{id}/terminate(force) hard-stop a stuck agent

Both terminate and kill accept `--final`: close the agent — never
auto-resurrected; `resurrect` reopens it.

`send` is the shell-level message primitive: the completion notices of
`ava.shell.run_background` and watcher exit notices are generated command lines
ending in `ava agents send ... --source shell:N|watcher:N`, and a host operator
can message any agent directly. A `send` that cannot reach the gateway is not
lost: the deferred-delivery outbox (`shared/delivery_outbox`) records it on this
machine and the ops daemon redelivers it once the gateway returns — the same
coverage the SDK send path has. Richer capabilities (spawn an agent, inspect its
events) stay in the `ava.*` SDK and the web UI.
"""

from __future__ import annotations

from pydantic import BaseModel

_TIMEOUT_S = 15.0

# The billing batch performs N launch-and-confirm cycles across home machines;
# 10 minutes bounds a fleet-scale recovery (23 agents in the 2026-09-18
# incident, dispatch concurrency 6) while still failing a wedged gateway well
# inside an operator's attention span.
_BATCH_TIMEOUT_S = 600.0


class ProvenanceError(ValueError):
    """A provenance requirement failed; the CLI reports it instead of a traceback.

    Raised where an explicit ``--source`` value is missing on a requiring verb
    or is malformed. Per-command CLI handlers catch it,
    print the message, and exit 2; the command layer itself keeps raising so
    callers and tests see one honest contract.
    """


def _validated_source_arg(source: str) -> str:
    """Validate one explicit source value (the CLI parse layer runs first)."""
    from shared.agents.messages.envelope import validate_source

    try:
        validate_source(source)
    except ValueError as exc:
        raise ProvenanceError(str(exc)) from exc
    return source


def _explicit_caller(source: str | None, *, field: str = "source") -> dict[str, str]:
    """Explicit provenance only — the environment never supplies or vetoes it.

    User ruling 2026-09-20: CLI parameters are explicit; the opt-in
    AVA_CALLER_IDENTITY profile (still consumed by SDK-side stamping, see
    ``ava._boot.default_actor``) no longer compensates an omitted ``--source``.
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
    from shared.http_dial import get as dial_get
    from shared.machine import gateway_api_base, gateway_auth_headers

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


# How much of a --tail-file is appended to the message (bytes read from the
# end; decoded with errors="replace" so a mid-character cut cannot break the
# POST). Fixed — a caller who needs more reads the file itself.
_TAIL_BYTES = 2048


_SEND_SOURCE_GUIDE = (
    "send requires --source (explicit provenance); nothing was sent.\n"
    "  --source user                  send as the user (the human operator)\n"
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
    completion_exit_code: int | None = None,
) -> int:
    """`ava agents send <id> <content> --source S [--tail-file PATH]` — deliver a
    chat inbound via POST /api/agents/{id}/messages.

    `--source` is required and validated at the argparse layer: a missing or
    unknown source is a usage error (exit 2) before any command code runs, and
    no environment fallback is consulted on this path (user ruling 2026-09-20:
    explicit parameters only). Machine callers pass `shell:N` / `watcher:N`; a
    human operator — or an agent acting as one — passes `user`. A source that
    still reaches the gateway is re-validated there (`AgentMessageIn.source` ->
    `shared.agents.messages.envelope.validate_source`) and rejected 422 with the legal set
    printed. A programmatic caller with no source gets this option list as a
    ProvenanceError, which the CLI handlers report without a traceback.

    `--tail-file` appends the last `_TAIL_BYTES` bytes of PATH to the message —
    the background-run / watcher completion notices use it to carry the end of
    the command's output (result line or traceback) so the agent usually does
    not need a follow-up read. Delivery auto-resurrects a terminated target
    (gateway behavior, same as the SDK path).

    A failed send is not lost: the deferred-delivery outbox
    (`shared/delivery_outbox`) records a transport failure or a transient HTTP
    response (429/5xx) on this machine under the message's idempotency key, and
    the machine's ops daemon redelivers it once the gateway returns. 4xx stay
    loud and unrecorded — the wire reason is application semantics, replay
    cannot change it."""
    # argparse enforces --source for the CLI; the guard covers programmatic callers.
    source = _explicit_send_source(source)
    status = send_agent_message(
        agent_id,
        content,
        source=source,
        tail_file=tail_file,
        completion_exit_code=completion_exit_code,
    )
    print(f"  ✓ agent {agent_id} send: {status}")
    return 0


def send_agent_message(
    agent_id: int,
    content: str,
    *,
    source: str,
    tail_file: str | None = None,
    completion_exit_code: int | None = None,
) -> str:
    """Deliver one chat inbound to `agent_id` carrying `source` — the single transport.

    Shared by `agents send` and `impersonate send` (the attested send as a
    leased identity): one idempotency keying, one deferred-delivery outbox
    safety net, one error surface.

    `--tail-file` appends the last `_TAIL_BYTES` bytes of PATH to the message —
    the background-run / watcher completion notices use it to carry the end of
    the command's output (result line or traceback) so the agent usually does
    not need a follow-up read.

    A failed send is not lost: the deferred-delivery outbox
    (`shared.agents.messages.delivery_outbox`) records a transport failure or a transient HTTP
    response (429/5xx) on this machine under the message's idempotency key, and
    the machine's ops daemon redelivers it once the gateway returns. 4xx stay
    loud and unrecorded — the wire reason is application semantics, replay
    cannot change it. Returns the gateway's delivery status; HTTP errors raise
    to the calling command."""
    import os
    import sys
    from pathlib import Path

    import httpx

    from shared.agents.messages import delivery_outbox
    from shared.http_dial import post as dial_post
    from shared.machine import gateway_api_base, gateway_auth_headers

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
    completion_notice: dict[str, object] | None = None
    if completion_exit_code is not None:
        completion_notice = {"outcome": "exit", "exit_code": completion_exit_code}
    # All attempts of one logical message share one key; minting it here also
    # arms the server's client_message_id receipt for the flush replay.
    key: str | None = None
    try:
        key = delivery_outbox.logical_key(
            agent_id=agent_id,
            source=source,
            content=content,
            completion_notice=completion_notice,
        )
    except Exception:
        # The outbox is a safety net for a failing send, never a reason for
        # one: an unusable outbox degrades to the unkeyed behavior with a
        # loud note, instead of changing the call's outcome.
        print(
            "warning: delivery outbox unavailable; sending an unkeyed message",
            file=sys.stderr,
        )
    headers = gateway_auth_headers()
    if key is not None:
        headers = {**headers, "Idempotency-Key": key}
    url = f"{gateway_api_base()}/api/agents/{agent_id}/messages"
    try:
        resp = dial_post(
            url,
            json={
                "content": content,
                "source": source,
                **({"completion_notice": completion_notice} if completion_notice else {}),
            },
            timeout=_TIMEOUT_S,
            headers=headers,
        )
    except httpx.TransportError:
        if key is not None:
            delivery_outbox.record_failed_send(
                agent_id=agent_id,
                source=source,
                content=content,
                client_message_id=key,
                completion_notice=completion_notice,
            )
        raise
    if key is not None:
        if resp.status_code in delivery_outbox.TRANSIENT_HTTP_STATUSES:
            delivery_outbox.record_failed_send(
                agent_id=agent_id,
                source=source,
                content=content,
                client_message_id=key,
                completion_notice=completion_notice,
            )
        elif resp.is_success:
            delivery_outbox.note_send_succeeded(
                agent_id=agent_id,
                source=source,
                content=content,
                key=key,
                completion_notice=completion_notice,
            )
    if resp.status_code >= 400:
        # Surface the response body before raising: the 422 detail carries the
        # legal source set / validation reason, which is the actionable part.
        print(resp.text, file=sys.stderr)
    resp.raise_for_status()
    return str(resp.json().get("status"))


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
    from shared.http_dial import post as dial_post
    from shared.machine import gateway_api_base, gateway_auth_headers

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


def cmd_agents_resurrect_billing(*, execute: bool) -> int:
    """`ava agents resurrect-billing` — POST /api/agents/resurrect-billing.

    The explicit post-outage recovery entry (task #3919): with no flags it
    prints a strictly read-only preview — the billing-class halt candidates,
    the halted-but-alive survey, and the provider balance readout; with
    `--execute` it runs the batch (balance gate -> per-agent
    `resurrect-billing-v1` on each home machine) and prints the per-agent
    outcome. Refused runs exit 1; previews and runs exit 0.
    """
    from shared.http_dial import post as dial_post
    from shared.machine import gateway_api_base, gateway_auth_headers

    url = f"{gateway_api_base()}/api/agents/resurrect-billing"
    resp = dial_post(
        url,
        json={"execute": execute},
        timeout=_BATCH_TIMEOUT_S,
        headers=gateway_auth_headers(),
    )
    resp.raise_for_status()
    data = resp.json()
    balance = data["balance"]
    print(f"  mode: {data['mode']} — outcome: {data['outcome']}")
    if data.get("refusal_reason"):
        print(f"  refused: {data['refusal_reason']}")
    print(f"  balance: ok={balance['ok']} — {balance['detail']}")
    agents = data["agents"]
    if not agents:
        print("  (no billing-class halt victims to resurrect)")
    for a in agents:
        suffix = f" — {a['reason']}" if a.get("reason") else ""
        print(f"  - agent {a['agent_id']} [{a['machine']}] {a['status']}{suffix}")
    halted = data.get("halted_alive") or []
    if halted:
        print(
            "  halted but alive — no action needed; the halt clears on their next successful turn:"
        )
        for a in halted:
            print(f"  - agent {a['agent_id']} [{a['machine']}] streak={a['streak']}")
    if not execute:
        print("  (preview only; rerun with --execute to perform)")
    return 1 if data.get("outcome") == "refused" else 0


def _terminate(
    agent_id: int, *, force: bool, source: str | None = None, final: bool = False
) -> int:
    """Shared POST for `terminate` (graceful) and `kill` (force) — both hit
    POST /api/agents/{id}/terminate, differing only in the `force` flag. An
    explicit source is forwarded unchanged; the environment is never consulted
    (user ruling 2026-09-20). Omitting it claims no provenance and
    leaves the server default in place.
    `final` closes the agent (never auto-resurrect); it is sent only when set,
    so an older gateway never receives a flag it cannot honor."""
    from shared.http_dial import post as dial_post
    from shared.machine import gateway_api_base, gateway_auth_headers

    verb = "kill" if force else "terminate"
    url = f"{gateway_api_base()}/api/agents/{agent_id}/terminate"
    resp = dial_post(
        url,
        json={"force": force, **({"final": True} if final else {}), **_explicit_caller(source)},
        timeout=_TIMEOUT_S,
        headers=gateway_auth_headers(),
    )
    resp.raise_for_status()
    data = resp.json()
    # `closed` rides only when the runner reports it (absent on older runners):
    # print the closure state when present, so an already-terminated `--final`
    # close is verifiable from the output alone.
    closed = data.get("closed")
    suffix = "" if closed is None else (" — closed" if closed else " — not closed")
    print(f"  ✓ agent {agent_id} {verb}: {data.get('status')}{suffix}")
    return 0


def cmd_agents_terminate(agent_id: int, *, source: str | None = None, final: bool = False) -> int:
    """`ava agents terminate <id>` — graceful stop: the agent exits after
    processing its current turn. For an agent wedged mid-turn (a hung step) that
    cannot reach the graceful exit, use `kill`. With `--final` the agent is also
    closed: never auto-resurrected (its queued work dead-letters on the existing
    thresholds); `ava agents resurrect <id>` reopens it. On an
    already-terminated agent `--final` is the metadata-only mark (the backfill
    route for agents closed before the marker existed); the output reports the
    resulting closure state."""
    return _terminate(agent_id, force=False, source=source, final=final)


def cmd_agents_kill(agent_id: int, *, source: str | None = None, final: bool = False) -> int:
    """`ava agents kill <id>` — request forceful interruption. Hosted work may
    return enqueued while it drains; this is acceptance, not observed exit.
    The response acknowledges the host lifecycle request; completion is asynchronous.
    `--final` also closes the agent (see `terminate --final`)."""
    return _terminate(agent_id, force=True, source=source, final=final)


def cmd_agents_compact(agent_id: int) -> int:
    """`ava agents compact <id>` — request conversation compaction through the
    gateway's durable `compact_request` inbound (the same surface the web UI
    triggers).

    Returns immediately: the agent consumes the request on its next claim pass.
    A terminated target is auto-resurrected first (except a closed one); a
    wedged target consumes it once recovered (turn-liveness restart, or an
    operator kill + resurrect) — the request is durable and waits."""
    from shared.http_dial import post as dial_post
    from shared.machine import gateway_api_base, gateway_auth_headers

    url = f"{gateway_api_base()}/api/agents/{agent_id}/compact"
    resp = dial_post(url, timeout=_TIMEOUT_S, headers=gateway_auth_headers())
    resp.raise_for_status()
    print(f"  ✓ agent {agent_id} compact: {resp.json().get('status')}")
    return 0
