"""`ava notices` — operator CLI for the agent notification queue (Task #949).

Thin client over the gateway's notice endpoints: list any agent's open
notices (both kinds), resolve them one by one, or clear an agent's backlog.
The gateway owns the effect; the CLI adds only rendering + arg parsing and
fails fast (raise_for_status()) on any HTTP error.

  list                 GET /api/notices/open?include_awaiting=1
                       (--agent / --priority / --type filters)
  resolve <id> --agent <id> --action answer|read|dismiss [--reply TEXT]
  clear --agent <id>   resolve every open notice (read for FYI, dismiss for
                       decisions; --force skips the confirmation)
"""

from __future__ import annotations

from typing import Any

_TIMEOUT_S = 15.0


def _dial() -> tuple[str, dict[str, str], Any, Any]:
    from shared.http_dial import get as dial_get
    from shared.http_dial import post as dial_post
    from shared.machine import gateway_api_base, gateway_auth_headers

    base = f"{gateway_api_base()}/api"
    headers = gateway_auth_headers()
    return base, headers, dial_get, dial_post


def _print_notice(
    n: dict[str, Any], i: int, total: int, *, agent_status: str | None = None
) -> None:
    kind = "DECISION" if n["require_response"] else "FYI"
    label = n.get("agent_label") or ""
    status = f" [{agent_status}]" if agent_status else ""
    print(
        f"[{i}/{total}] #{n['id']} {kind} {n['priority']} "
        f"agent#{n['agent_id']} {label}{status}: {n['title']}"
    )
    if n.get("content"):
        print(f"    {n['content'][:120]}")


def _agent_status_map(base: str, headers: dict[str, str], dial_get: Any) -> dict[int, str]:
    """agent_id -> status for every agent (terminated ones included).

    The open-notices feed does not carry agent liveness; a stale-backlog
    cleanup (Task #1149) needs it to tell terminated agents' notices — the
    invalid backlog — from live agents' open notices, which stay put.
    """
    resp = dial_get(f"{base}/agents", timeout=_TIMEOUT_S, headers=headers)
    resp.raise_for_status()
    return {a["agent_id"]: a["status"] for a in resp.json()}


def cmd_notices_list(
    *,
    agent_id: int | None,
    priority: str | None,
    type_filter: str | None,
    stale: bool = False,
) -> int:
    """`ava notices list` — every open notice across the fleet (or one agent).

    --stale narrows to notices whose agent is terminated: the invalid
    backlog of dead agents, as opposed to live agents' open notices
    (Task #1149)."""
    base, headers, dial_get, _ = _dial()
    resp = dial_get(
        f"{base}/notices/open",
        timeout=_TIMEOUT_S,
        headers=headers,
        params={"include_awaiting": True, "limit": 200},
    )
    resp.raise_for_status()
    notices = resp.json()
    if agent_id is not None:
        notices = [n for n in notices if n["agent_id"] == agent_id]
    statuses: dict[int, str] = {}
    if stale:
        statuses = _agent_status_map(base, headers, dial_get)
        notices = [n for n in notices if statuses.get(n["agent_id"]) == "terminated"]
    if priority:
        notices = [n for n in notices if n["priority"] == priority.upper()]
    if type_filter == "fyi":
        notices = [n for n in notices if not n["require_response"]]
    elif type_filter == "decision":
        notices = [n for n in notices if n["require_response"]]
    if not notices:
        print("No open notices." if not stale else "No stale notices (terminated agents).")
        return 0
    total = len(notices)
    for i, n in enumerate(notices, start=1):
        _print_notice(n, i, total, agent_status=statuses.get(n["agent_id"]) if stale else None)
    return 0


def cmd_notices_resolve(*, notice_id: int, agent_id: int, action: str, reply: str | None) -> int:
    """`ava notices resolve <id> --agent <id> --action ...` — resolve one notice."""
    if action not in ("answer", "read", "dismiss"):
        print(f"invalid action {action!r} (answer|read|dismiss)")
        return 2
    if action == "answer" and not reply:
        print("--reply TEXT is required for action=answer")
        return 2
    base, headers, _, dial_post = _dial()
    resp = dial_post(
        f"{base}/agents/{agent_id}/notices/{notice_id}/resolve",
        timeout=_TIMEOUT_S,
        headers=headers,
        json={"action": action, "reply": reply},
    )
    if resp.status_code == 409:
        print(f"notice #{notice_id} is already resolved (409)")
        return 0
    resp.raise_for_status()
    print(f"notice #{notice_id} resolved: {action}")
    return 0


def cmd_notices_clear(*, agent_id: int | None, force: bool, stale: bool = False) -> int:
    """`ava notices clear --agent <id>` — resolve every open notice of one agent.

    FYI notices get read, decisions get dismissed (answers need a human).
    Lists what it will clear and asks for confirmation unless --force.

    --stale clears every open notice of terminated agents instead (mutually
    exclusive with --agent): the invalid backlog, keeping live agents' open
    notices untouched (Task #1149)."""
    if stale and agent_id is not None:
        print("--stale and --agent are mutually exclusive")
        return 2
    base, headers, dial_get, dial_post = _dial()
    resp = dial_get(
        f"{base}/notices/open",
        timeout=_TIMEOUT_S,
        headers=headers,
        params={"include_awaiting": True, "limit": 200},
    )
    resp.raise_for_status()
    notices = resp.json()
    if stale:
        statuses = _agent_status_map(base, headers, dial_get)
        notices = [n for n in notices if statuses.get(n["agent_id"]) == "terminated"]
        if not notices:
            print("No stale notices (terminated agents).")
            return 0
    else:
        notices = [n for n in notices if n["agent_id"] == agent_id]
        if not notices:
            print(f"agent #{agent_id}: no open notices.")
            return 0
    if not force:
        for i, n in enumerate(notices, start=1):
            _print_notice(n, i, len(notices))
        target = "stale (terminated-agent) notices" if stale else f"agent #{agent_id}"
        print(
            f"Clear {len(notices)} notice(s): {target}? "
            "(FYI -> read, decision -> dismiss; re-run with --force to skip this prompt)"
        )
        try:
            answer = input("y/N: ").strip().lower()
        except EOFError:
            answer = "n"
        if answer not in ("y", "yes"):
            print("aborted.")
            return 1
    cleared = 0
    for n in notices:
        action = "read" if not n["require_response"] else "dismiss"
        r = dial_post(
            f"{base}/agents/{n['agent_id']}/notices/{n['id']}/resolve",
            timeout=_TIMEOUT_S,
            headers=headers,
            json={"action": action, "reply": None},
        )
        if r.status_code in (200, 201):
            cleared += 1
    target = "stale notices" if stale else f"agent #{agent_id}"
    print(f"cleared {cleared}/{len(notices)} notices: {target}")
    return 0
