"""`ava schedules` — manage gateway-supervised schedules (thin client over the gateway).

A schedule is a persistent session (a `script` + the `command` that runs it) the
gateway's ScheduleManager keeps alive in a pty session. This is the
shell-level management surface: list / get / create / update / delete plus the control verbs
(start / stop / restart) and the two read-only observation verbs (logs / runs).

Each verb forwards to an existing route under `/api/schedules` and renders the
result — the gateway owns the effect (script syntax check, version snapshot,
session reconcile), the CLI adds only arg parsing + rendering. This is the
operator/agent primitive form: `POST /api/schedules/draft` (hand a natural-
language request to a schedule_writer agent) is deliberately NOT exposed here —
the CLI writes a script it already has, it does not run a conversation.

Every verb that names a schedule accepts a name or a numeric id.
"""

from __future__ import annotations

import argparse
import contextlib
import sys
from pathlib import Path
from typing import Any

import httpx

_TIMEOUT_S = 15.0


def _gateway_base() -> str:
    from shared.machine import gateway_api_base

    return gateway_api_base()


def _headers() -> dict[str, str]:
    from shared.machine import gateway_auth_headers

    return gateway_auth_headers()


# ── list ──


def cmd_schedules_ls() -> int:
    """`ava schedules ls` — list every schedule (id / enabled / status / name).

    Disabled schedules are listed too; `status` is the ScheduleManager's live
    supervision state, `enabled` is the desired state stored on the row."""
    from shared.http_dial import get as dial_get

    resp = dial_get(f"{_gateway_base()}/api/schedules", timeout=_TIMEOUT_S, headers=_headers())
    resp.raise_for_status()
    rows = resp.json()

    if not rows:
        print("(no schedules)")
        return 0

    id_w = max(len("id"), *(len(str(r["id"])) for r in rows))
    status_w = max(len("status"), *(len(r["status"]) for r in rows))
    print(f"{'id'.rjust(id_w)}  {'on':<3}  {'status'.ljust(status_w)}  name")
    for r in rows:
        on = "yes" if r["enabled"] else "no"
        print(f"{str(r['id']).rjust(id_w)}  {on:<3}  {r['status'].ljust(status_w)}  {r['name']}")
    return 0


# ── get ──


def cmd_schedules_get(identifier: str) -> int:
    """`ava schedules get <name-or-id>` — the full schedule including its script."""
    row = _fetch(identifier)
    if row is None:
        return 1
    _print_schedule(row, with_script=True)
    return 0


# ── create ──


def cmd_schedules_create(
    name: str,
    script: str | None,
    script_file: str | None,
    command: str | None,
    description: str | None,
    *,
    disabled: bool = False,
) -> int:
    """`ava schedules create --name N (--script S | --script-file F) [...]`.

    Exactly one of --script / --script-file supplies the body (`--script-file -`
    reads stdin, the natural form for a heredoc). The gateway `compile()`-checks
    the script and rejects a SyntaxError with 400, so a broken script never
    reaches the runner. Created enabled unless --disabled; an enabled schedule is
    launched by the reconcile loop within a poll interval."""
    from shared.http_dial import post as dial_post

    source = _read_script(script, script_file)
    if source is None:
        return 1

    body: dict[str, object] = {"name": name, "script": source, "enabled": not disabled}
    if command is not None:
        body["command"] = command
    if description is not None:
        body["description"] = description

    resp = dial_post(
        f"{_gateway_base()}/api/schedules", json=body, timeout=_TIMEOUT_S, headers=_headers()
    )
    if resp.status_code == 409:
        print(f"schedule named {name!r} already exists", file=sys.stderr)
        return 1
    if resp.status_code == 400:
        print(_detail(resp), file=sys.stderr)
        return 1
    _die_on_unexpected_error(resp)
    row = resp.json()
    print(f"  ✓ created schedule #{row['id']} ({row['name']})")
    _print_schedule(row, with_script=False)
    return 0


# ── update ──


def cmd_schedules_update(
    identifier: str,
    name: str | None,
    script: str | None,
    script_file: str | None,
    command: str | None,
    description: str | None,
    *,
    enabled: bool | None,
) -> int:
    """`ava schedules update <name-or-id> [--name N] [--script S | --script-file F] ...`.

    Partial update — only the flags passed change (PUT with an exclude-unset
    body). A script/command change is snapshotted into `schedule_versions` and,
    when the schedule is enabled, the session is relaunched onto the new script
    immediately. --enable / --disable converge when they change desired state:
    the same field `start`/`stop` flip is paired with session reaping or
    creation before the command returns, while a same-value update is a no-op."""
    from shared.http_dial import put as dial_put

    schedule_id = _resolve_id(identifier)
    if schedule_id is None:
        return 1

    body: dict[str, object] = {}
    if name is not None:
        body["name"] = name
    if script is not None or script_file is not None:
        source = _read_script(script, script_file)
        if source is None:
            return 1
        body["script"] = source
    if command is not None:
        body["command"] = command
    if description is not None:
        body["description"] = description
    if enabled is not None:
        body["enabled"] = enabled

    if not body:
        print(
            "at least one of --name / --script / --script-file / --command / "
            "--description / --enable / --disable is required",
            file=sys.stderr,
        )
        return 1

    resp = dial_put(
        f"{_gateway_base()}/api/schedules/{schedule_id}",
        json=body,
        timeout=_TIMEOUT_S,
        headers=_headers(),
    )
    if resp.status_code in (400, 404, 409):
        print(_detail(resp), file=sys.stderr)
        return 1
    _die_on_unexpected_error(resp)
    row = resp.json()
    print(f"  ✓ updated schedule #{row['id']} ({row['name']})")
    _print_schedule(row, with_script=False)
    return 0


# ── delete ──


def cmd_schedules_delete(identifier: str, *, force: bool = False) -> int:
    """`ava schedules delete <name-or-id> [--force]`.

    Prompts unless --force. The gateway kills the now-orphaned session and
    removes the schedule's work dir; its run history goes with the row."""
    from shared.http_dial import delete as dial_delete

    schedule_id = _resolve_id(identifier)
    if schedule_id is None:
        return 1

    if not force:
        answer = input(f"Delete schedule {identifier!r} (id={schedule_id})? [y/N] ")
        if answer.strip().lower() not in ("y", "yes"):
            print("cancelled")
            return 0

    resp = dial_delete(
        f"{_gateway_base()}/api/schedules/{schedule_id}", timeout=_TIMEOUT_S, headers=_headers()
    )
    if resp.status_code == 404:
        print(f"schedule {schedule_id} not found", file=sys.stderr)
        return 1
    resp.raise_for_status()
    print(f"  ✓ deleted schedule #{schedule_id}")
    return 0


# ── control ──


def _control(identifier: str, verb: str) -> int:
    """Shared POST for start / stop / restart — same shape, different sub-path."""
    from shared.http_dial import post as dial_post

    schedule_id = _resolve_id(identifier)
    if schedule_id is None:
        return 1

    resp = dial_post(
        f"{_gateway_base()}/api/schedules/{schedule_id}/{verb}",
        timeout=_TIMEOUT_S,
        headers=_headers(),
    )
    if resp.status_code in (404, 409):
        print(_detail(resp), file=sys.stderr)
        return 1
    _die_on_unexpected_error(resp)
    row = resp.json()
    print(f"  ✓ schedule #{row['id']} {verb}: enabled={row['enabled']} status={row['status']}")
    return 0


def cmd_schedules_start(identifier: str) -> int:
    """`ava schedules start <name-or-id>` — enable + launch the session now."""
    return _control(identifier, "start")


def cmd_schedules_stop(identifier: str) -> int:
    """`ava schedules stop <name-or-id>` — disable + kill the session now."""
    return _control(identifier, "stop")


def cmd_schedules_restart(identifier: str) -> int:
    """`ava schedules restart <name-or-id>` — relaunch on the current script,
    clearing crash backoff. 409 if the schedule is disabled (use `start`)."""
    return _control(identifier, "restart")


# ── observation ──


def cmd_schedules_logs(identifier: str, lines: int) -> int:
    """`ava schedules logs <name-or-id> [--lines N]` — recent output.

    Source is the live session capture when a session is up, the session's
    PTY transcript file when it is gone (a finished/crashed runner's output
    survives there), `last_error` (the last crash traceback), or `none`."""
    from shared.http_dial import get as dial_get

    schedule_id = _resolve_id(identifier)
    if schedule_id is None:
        return 1

    resp = dial_get(
        f"{_gateway_base()}/api/schedules/{schedule_id}/logs",
        params={"lines": lines},
        timeout=_TIMEOUT_S,
        headers=_headers(),
    )
    if resp.status_code == 404:
        print(f"schedule {schedule_id} not found", file=sys.stderr)
        return 1
    resp.raise_for_status()
    view = resp.json()
    if view["source"] == "none":
        print("(no output yet — no live session and no recorded error)")
        return 0
    print(f"── source: {view['source']} ──")
    for line in view["lines"]:
        print(line)
    return 0


def cmd_schedules_runs(identifier: str, limit: int) -> int:
    """`ava schedules runs <name-or-id> [--limit N]` — run history, newest first."""
    from shared.http_dial import get as dial_get

    schedule_id = _resolve_id(identifier)
    if schedule_id is None:
        return 1

    resp = dial_get(
        f"{_gateway_base()}/api/schedules/{schedule_id}/runs",
        params={"limit": limit},
        timeout=_TIMEOUT_S,
        headers=_headers(),
    )
    if resp.status_code == 404:
        print(f"schedule {schedule_id} not found", file=sys.stderr)
        return 1
    resp.raise_for_status()
    rows = resp.json()
    if not rows:
        print("(no runs yet)")
        return 0
    print(f"{'ran_at':<28}  {'ok':<5}  {'agent':<6}  note")
    for r in rows:
        ok = "-" if r["ok"] is None else ("yes" if r["ok"] else "no")
        agent = "-" if r["agent_id"] is None else str(r["agent_id"])
        print(f"{r['ran_at']:<28}  {ok:<5}  {agent:<6}  {r['note'] or ''}")
    return 0


# ── helpers ──


def cmd_schedules_provision() -> int:
    """`ava schedules provision` — create the built-in schedules missing from
    this cluster, per schedules/manifest.json (product schedules —
    self-evolution, memory — enabled; cluster-operator schedules — e.g.
    trace-ship-tempo — present but disabled).

    Unlike the other `ava schedules` verbs this writes the DB directly instead
    of going through the gateway API: it is an initialization/restore action
    that must also work while the gateway is down (and the gateway itself runs
    the same provision at every boot). Idempotent — existing schedules (by
    name) are never modified, so an operator's edits survive a provision."""
    import shared.db
    from shared.builtin_schedules import provision_builtin_schedules

    with shared.db.connect(autocommit=True) as conn:
        created = provision_builtin_schedules(conn)
    if created:
        print(f"provisioned built-in schedules: {', '.join(created)}")
    else:
        print("(all built-in schedules already present)")
    return 0


def _read_script(script: str | None, script_file: str | None) -> str | None:
    """Resolve the script body from --script / --script-file (`-` = stdin).

    Exactly one source must be given; returns None + prints to stderr otherwise
    (or if the file is unreadable)."""
    if (script is None) == (script_file is None):
        print("exactly one of --script / --script-file is required", file=sys.stderr)
        return None
    if script is not None:
        return script
    assert script_file is not None  # noqa: S101 — narrowed by the XOR check above
    if script_file == "-":
        return sys.stdin.read()
    try:
        return Path(script_file).read_text(encoding="utf-8")
    except OSError as e:
        print(f"cannot read script file: {e}", file=sys.stderr)
        return None


def _resolve_id(identifier: str) -> int | None:
    """Resolve a name-or-id string to a numeric schedule id. Returns None +
    prints to stderr when the name matches nothing."""
    from shared.http_dial import get as dial_get

    with contextlib.suppress(ValueError):
        return int(identifier)

    resp = dial_get(f"{_gateway_base()}/api/schedules", timeout=_TIMEOUT_S, headers=_headers())
    resp.raise_for_status()
    match = next((r for r in resp.json() if r["name"] == identifier), None)
    if match is None:
        print(f"schedule named {identifier!r} not found", file=sys.stderr)
        return None
    return int(match["id"])


def _fetch(identifier: str) -> dict[str, Any] | None:
    """GET one full schedule (with script) by name or id."""
    from shared.http_dial import get as dial_get

    schedule_id = _resolve_id(identifier)
    if schedule_id is None:
        return None
    resp = dial_get(
        f"{_gateway_base()}/api/schedules/{schedule_id}", timeout=_TIMEOUT_S, headers=_headers()
    )
    if resp.status_code == 404:
        print(f"schedule {schedule_id} not found", file=sys.stderr)
        return None
    resp.raise_for_status()
    return resp.json()


def _detail(resp: httpx.Response) -> str:
    """The gateway's `detail` string, falling back to the raw body — the
    actionable part of a 400/404/409 (syntax error line, name clash, ...)."""
    with contextlib.suppress(ValueError, TypeError):
        payload: dict[str, Any] = resp.json()
        if isinstance(payload, dict) and "detail" in payload:
            return str(payload["detail"])
    return str(resp.text)


def _die_on_unexpected_error(resp: httpx.Response) -> None:
    """Print the body of an error the caller did not classify, then raise.

    The verbs handle the statuses the router documents (400/404/409); anything
    else — a 422 body-validation failure, a 5xx — would otherwise surface as a
    bare httpx traceback with the response body swallowed. Printing first keeps
    the gateway's reason visible while still failing loudly."""
    if resp.status_code >= 400:
        print(resp.text, file=sys.stderr)
    resp.raise_for_status()


def _print_schedule(row: dict[str, Any], *, with_script: bool) -> None:
    """Pretty-print a schedule row. `with_script` includes the (potentially
    large) script body — on for `get`, off for the create/update echo."""
    print(f"  id:          {row['id']}")
    print(f"  name:        {row['name']}")
    if row.get("description"):
        print(f"  description: {row['description']}")
    print(f"  command:     {row['command']}")
    print(f"  enabled:     {row['enabled']}")
    print(f"  status:      {row['status']}")
    if row.get("last_error"):
        print(f"  last_error:  {row['last_error'].splitlines()[-1]}")
    if with_script:
        print("  script:")
        for line in row["script"].splitlines():
            print(f"    {line}")


# ── argparse handlers (wired by the cli/parsers tree) ──


def h_schedules_ls(_args: argparse.Namespace) -> int:
    return cmd_schedules_ls()


def h_schedules_get(args: argparse.Namespace) -> int:
    return cmd_schedules_get(args.identifier)


def h_schedules_create(args: argparse.Namespace) -> int:
    return cmd_schedules_create(
        args.name,
        args.script,
        args.script_file,
        args.command,
        args.description,
        disabled=args.disabled,
    )


def h_schedules_update(args: argparse.Namespace) -> int:
    # --enable / --disable are one tri-state: None = leave the flag alone.
    enabled = True if args.enable else (False if args.disable else None)
    return cmd_schedules_update(
        args.identifier,
        args.name,
        args.script,
        args.script_file,
        args.command,
        args.description,
        enabled=enabled,
    )


def h_schedules_delete(args: argparse.Namespace) -> int:
    return cmd_schedules_delete(args.identifier, force=args.force)


def h_schedules_start(args: argparse.Namespace) -> int:
    return cmd_schedules_start(args.identifier)


def h_schedules_stop(args: argparse.Namespace) -> int:
    return cmd_schedules_stop(args.identifier)


def h_schedules_restart(args: argparse.Namespace) -> int:
    return cmd_schedules_restart(args.identifier)


def h_schedules_logs(args: argparse.Namespace) -> int:
    return cmd_schedules_logs(args.identifier, args.lines)


def h_schedules_runs(args: argparse.Namespace) -> int:
    return cmd_schedules_runs(args.identifier, args.limit)


def h_schedules_provision(_args: argparse.Namespace) -> int:
    return cmd_schedules_provision()
