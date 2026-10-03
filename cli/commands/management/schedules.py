"""`ava schedules` — manage gateway-supervised schedules (thin client over the gateway).

A schedule is a persistent session (a `script` + the `command` that runs it) the
gateway's ScheduleManager keeps alive in a pty session. This is the
shell-level management surface: list / get / create / update / delete plus the control verbs
(start / stop / restart), the two read-only observation verbs (logs / runs), and the local
`verify` dry-import sweep.

Most verbs forward to an existing route under `/api/schedules` and render the
result — the gateway owns the effect (script syntax check, version snapshot,
session reconcile), the CLI adds only arg parsing + rendering. This is the
operator/agent primitive form: `POST /api/schedules/draft` (hand a natural-
language request to a schedule_writer agent) is deliberately NOT exposed here —
the CLI writes a script it already has, it does not run a conversation. Two
verbs never touch the gateway: `provision` (restores the built-ins) and
`verify` (dry-imports the DB's in-store scripts against this checkout), so both
keep working while the gateway is down.

Every verb that names a schedule accepts a name or a numeric id.
"""

from __future__ import annotations

import argparse
import contextlib
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

_TIMEOUT_S = 15.0


def _gateway_base() -> str:
    from base.cluster.machine import gateway_api_base

    return gateway_api_base()


def _headers() -> dict[str, str]:
    from base.cluster.machine import gateway_auth_headers

    return gateway_auth_headers()


# ── list ──


def cmd_schedules_ls() -> int:
    """`ava schedules ls` — list every schedule (id / enabled / status / name).

    Disabled schedules are listed too; `status` is the ScheduleManager's live
    supervision state, `enabled` is the desired state stored on the row."""
    from base.host.net.http_dial import get as dial_get

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
    from base.host.net.http_dial import post as dial_post

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
    from base.host.net.http_dial import put as dial_put

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
    from base.host.net.http_dial import delete as dial_delete

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
    from base.host.net.http_dial import post as dial_post

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
    from base.host.net.http_dial import get as dial_get

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
    from base.host.net.http_dial import get as dial_get

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


# ── verify ──


# One dry-import child, run as `python -c` per script: it compile()s the script
# and executes ONLY the top-level import statements, so a moved module path
# (the #2678 / R3 Wave-2 drift class) fails without the body ever running.
_VERIFY_CHILD = r"""import ast, sys
src = sys.stdin.read()
try:
    compile(src, "<schedule>", "exec")
    tree = ast.parse(src)
except SyntaxError as exc:
    print("CHILD-COMPILE-ERROR:%s:%s" % (exc.lineno, exc.msg))
    sys.exit(3)
segments = []
for node in tree.body:
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        segment = ast.get_source_segment(src, node)
        if segment:
            segments.append(segment)
try:
    exec(compile("\n".join(segments), "<imports-only>", "exec"), {"__name__": "dryimport"})
except ModuleNotFoundError as exc:
    print("CHILD-MODULE:%s" % exc.name)
    sys.exit(4)
except Exception as exc:
    print("CHILD-EXC:%s:%s" % (type(exc).__name__, str(exc)[:120].replace("\n", " ")))
    sys.exit(5)
print("CHILD-OK")
"""

# One child's budget. task #3696 exception inventory: a self-imposed guard on a
# single check child — a healthy top-level import set takes seconds, so a child
# still running at 90s is wedged and must not park the sweep behind it.
_VERIFY_CHILD_TIMEOUT_S = 90.0

# The stable identity of the sweep's alert instance (one drift episode per
# cluster). Severity is fixed, so the fingerprint covers the label pair.
_VERIFY_ALERTNAME = "schedule dry-import"

# Delivery attempts for one alert POST. task #3696 exception inventory: fixed
# by the 2026-08-25 alert-delivery ruling (design #1595) — retry once, then drop; no direct-DB
# fallback for non-agent emitters.
_VERIFY_ALERT_ATTEMPTS = 2


def _check_script(script: str) -> str | None:
    """Dry-import one in-store script; None = clean, else the RED detail.

    Runs with this CLI's interpreter from the repo root: every sanctioned `ava`
    invocation is the checkout's own venv — the same `.venv/bin/python` the
    ScheduleManager launches the runner with.
    """
    from base.paths import repo_root

    try:
        proc = subprocess.run(
            [sys.executable, "-c", _VERIFY_CHILD],
            check=False,
            input=script,
            cwd=str(repo_root()),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=_VERIFY_CHILD_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return f"timeout>{_VERIFY_CHILD_TIMEOUT_S:.0f}s"
    except Exception as exc:  # spawn/OS failures surface as a red item
        return f"tool-error:{type(exc).__name__}:{str(exc)[:100]}"
    lines = [line for line in (proc.stdout or "").splitlines() if line.strip()]
    last = lines[-1] if lines else ""
    if last == "CHILD-OK":
        return None
    if last.startswith("CHILD-COMPILE-ERROR:"):
        return "compile-error:" + last[len("CHILD-COMPILE-ERROR:") :]
    if last.startswith("CHILD-MODULE:"):
        return last[len("CHILD-MODULE:") :]
    if last.startswith("CHILD-EXC:"):
        return last[len("CHILD-EXC:") :]
    tail = ((proc.stderr or "").strip().splitlines() or [""])[-1][:140]
    return f"tool-error:{tail or f'rc={proc.returncode}'}"


def _read_schedule_rows() -> list[tuple[int, str, str]]:
    """All schedules as (id, name, script) — stopped rows included.

    A direct read (not `/api/schedules`) so the check works while the gateway is
    down: the DB is the authority for what a runner materializes.
    """
    from base.db import Database

    with Database.from_settings().connect(autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("SELECT id, name, script FROM schedules ORDER BY id")
        return [(row[0], row[1], row[2] or "") for row in cur.fetchall()]


def _verify_sweep(*, notify: bool) -> int:
    """Run the dry-import sweep over every in-store script. Returns the exit code."""
    from base.clock import Clock

    stamp = datetime.now(Clock.from_settings().zone()).isoformat(timespec="seconds")
    try:
        rows = _read_schedule_rows()
    except Exception as exc:  # the tool-error path
        detail = f"{type(exc).__name__}: {str(exc)[:160]}"
        print(f"RESULT ts={stamp} checked=0 green=0 red=0 rc=2")
        print(f"TOOL-ERROR {detail}")
        if notify:
            _alert_verify(stamp=stamp, checked=0, reds=[], tool_error=detail)
        return 2

    reds: list[tuple[int, str, str]] = []
    checked = 0
    green = 0
    for schedule_id, name, script in rows:
        checked += 1
        if not script.strip():
            green += 1  # nothing to dry-import (empty / non-Python row): not a red
            continue
        missing = _check_script(script)
        if missing is None:
            green += 1
        else:
            reds.append((schedule_id, name, missing))
    rc = 1 if reds else 0
    print(f"RESULT ts={stamp} checked={checked} green={green} red={len(reds)} rc={rc}")
    for schedule_id, name, missing in reds:
        print(f"RED id={schedule_id} name={name} missing={missing}")
    if notify:
        _alert_verify(stamp=stamp, checked=checked, reds=reds, tool_error=None)
    return rc


def _verify_file(path: str) -> int:
    """`--check-file PATH` — dry-import one script file (no DB, no alert)."""
    try:
        source = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        print(f"cannot read script file: {exc}", file=sys.stderr)
        return 2
    missing = _check_script(source)
    print("CHECK-OK" if missing is None else f"CHECK-RED missing={missing}")
    return 0 if missing is None else 1


def cmd_schedules_verify(*, check_file: str | None = None, notify: bool = True) -> int:
    """`ava schedules verify [--check-file PATH] [--no-notify]` — the dry-import sweep.

    Read-only one-shot check of every in-store script (stopped rows included):
    py_compile plus a top-level-imports-only execution in this checkout's
    runner venv — nothing is started, stopped, or written. Catches the drift
    class where a repo module move leaves DB-embedded scripts stale and the
    next (re)start crash-loops (task #4800: #2678, the 2026-09-25 R3 Wave-2
    regression). Line contract: one `RESULT ts=... checked=... green=... red=...
    rc=...` line, one `RED id=... name=... missing=...` per red, `TOOL-ERROR
    ...` on rc=2; exit codes 0 all clean / 1 red / 2 tool error. `--check-file`
    checks one file off-DB (the falsification hook); a non-clean sweep alerts
    through `/api/alerts` unless `--no-notify`."""
    if check_file is not None:
        return _verify_file(check_file)
    return _verify_sweep(notify=notify)


def _open_verify_starts_at() -> str | None:
    """`starts_at` of the open (unresolved) verify alert, or None.

    The alerts table is the episode state (same derivation as the machine
    liveness pass): reusing an open instance's `starts_at` lets the ingest's
    notified_at gate keep a repeated red run from re-paging."""
    from base.db import Database

    with Database.from_settings().connect(autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT starts_at FROM alerts WHERE labels->>'alertname' = %s "
            "AND status = 'unresolved' ORDER BY starts_at DESC LIMIT 1",
            (_VERIFY_ALERTNAME,),
        )
        row = cur.fetchone()
    return row[0].isoformat() if row is not None else None


def _alert_verify(
    *,
    stamp: str,
    checked: int,
    reds: list[tuple[int, str, str]],
    tool_error: str | None,
) -> None:
    """Report a non-clean sweep through the alerts ingest — best-effort.

    Alert is the sanctioned system -> human surface for non-agent emitters (the
    health probe posts the same shape): one firing instance per drift episode,
    refreshed by later red runs and resolved by a clean one. Never raises —
    the caller already sees the result on stdout."""
    from base.cluster import home_label
    from base.paths import ava_home
    from base.telemetry.alerts import fingerprint

    try:
        labels = {"alertname": _VERIFY_ALERTNAME, "severity": "error"}
        starts_at = _open_verify_starts_at()
        label = home_label(ava_home())
        if reds or tool_error is not None:
            if tool_error is not None:
                detail = f"tool-error: {tool_error}"
            else:
                listing = "; ".join(
                    f"id={sid} name={name} missing={missing}" for sid, name, missing in reds
                )
                detail = (
                    f"{len(reds)}/{checked} in-store schedule script(s) "
                    f"failed dry-import: {listing}"
                )
            alert = {
                "status": "firing",
                "labels": labels,
                "annotations": {"summary": f"[{label}] [schedule-verify] {detail}"},
                "startsAt": starts_at or stamp,
                "endsAt": "",
                "fingerprint": fingerprint(labels),
            }
        elif starts_at is not None:
            alert = {
                "status": "resolved",
                "labels": labels,
                "annotations": {
                    "summary": (
                        f"[{label}] [schedule-verify] all {checked} in-store schedule "
                        "scripts dry-import clean"
                    )
                },
                "startsAt": starts_at,
                "endsAt": stamp,
                "fingerprint": fingerprint(labels),
            }
        else:
            return  # a clean run with no open episode stays silent
        _post_verify_alert(alert)
    except Exception as exc:  # alerting must never break the sweep
        print(f"  (verify alert failed: {type(exc).__name__}: {exc})", file=sys.stderr)


def _post_verify_alert(alert: dict[str, Any]) -> None:
    """POST one alert to the gateway's `/api/alerts` funnel; retry once, then drop."""
    payload = {"source": "schedule-verify", "alerts": [alert]}
    last_error: Exception | None = None
    for _attempt in range(_VERIFY_ALERT_ATTEMPTS):
        try:
            resp = httpx.post(
                f"{_gateway_base()}/api/alerts",
                json=payload,
                headers=_headers(),
                timeout=_TIMEOUT_S,
            )
            resp.raise_for_status()
            return
        except Exception as exc:  # retried once, then dropped
            last_error = exc
    if isinstance(last_error, httpx.HTTPStatusError):
        # Log the status + body only, never the exception: its repr embeds the
        # request, whose Authorization header must stay out of stderr.
        print(
            f"  (verify alert delivery failed: HTTP {last_error.response.status_code} "
            f"{last_error.response.text[:120]})",
            file=sys.stderr,
        )
    else:
        print(f"  (verify alert delivery failed: {type(last_error).__name__})", file=sys.stderr)


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
    from base.daemon.schedules.builtin_schedules import provision_builtin_schedules
    from base.db import Database

    with Database.from_settings().write_transaction() as conn:
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
    from base.host.net.http_dial import get as dial_get

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
    from base.host.net.http_dial import get as dial_get

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


def h_schedules_verify(args: argparse.Namespace) -> int:
    return cmd_schedules_verify(check_file=args.check_file, notify=not args.no_notify)
