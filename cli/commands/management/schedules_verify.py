"""`ava schedules verify` — the dry-import + call-signature sweep over every in-store schedule script."""

from __future__ import annotations

import argparse
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from cli.commands.management.schedules import _TIMEOUT_S, _gateway_base, _headers

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


# The child's last stdout line -> the RED detail (prefix added to the line's remainder).
_CHILD_VERDICTS = (
    ("CHILD-COMPILE-ERROR:", "compile-error:"),
    ("CHILD-MODULE:", ""),
    ("CHILD-EXC:", ""),
    ("CHILD-SIG:", "call-signature:"),
)


def _check_script(script: str) -> str | None:
    """Dry-import one in-store script and bind its repo-API call sites; None = clean, else the
    RED detail (`<module>` / `compile-error:<l>:<m>` / `<Exc>:<msg>` / `call-signature:<l> <call>: <why>`).

    Runs the check child (`schedule_verify_child`) with this CLI's interpreter from the repo
    root: every sanctioned `ava` invocation is the checkout's own venv — the same
    `.venv/bin/python` the ScheduleManager launches the runner with.
    """
    from base.paths import repo_root

    try:
        proc = subprocess.run(
            [sys.executable, "-m", "cli.commands.management.schedule_verify_child"],
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
    for verdict, prefix in _CHILD_VERDICTS:
        if last.startswith(verdict):
            return prefix + last[len(verdict) :]
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


@dataclass(frozen=True)
class VerifyPorts:
    """What a verify sweep reads and does: the schedule rows, the per-script dry
    import and the alert report. `cmd_schedules_verify` wires the real ones."""

    read_rows: Callable[[], list[tuple[int, str, str]]]
    check_script: Callable[[str], str | None]
    alert: Callable[..., None]


def _verify_sweep(*, notify: bool, ports: VerifyPorts) -> int:
    """Run the dry-import and call-signature sweep over every in-store script. Returns the exit code."""
    from base.clock import Clock

    stamp = datetime.now(Clock.from_settings().zone()).isoformat(timespec="seconds")
    try:
        rows = ports.read_rows()
    except Exception as exc:  # the tool-error path
        detail = f"{type(exc).__name__}: {str(exc)[:160]}"
        print(f"RESULT ts={stamp} checked=0 green=0 red=0 rc=2")
        print(f"TOOL-ERROR {detail}")
        if notify:
            ports.alert(stamp=stamp, checked=0, reds=[], tool_error=detail)
        return 2

    reds: list[tuple[int, str, str]] = []
    checked = 0
    green = 0
    for schedule_id, name, script in rows:
        checked += 1
        if not script.strip():
            green += 1  # nothing to dry-import (empty / non-Python row): not a red
            continue
        missing = ports.check_script(script)
        if missing is None:
            green += 1
        else:
            reds.append((schedule_id, name, missing))
    rc = 1 if reds else 0
    print(f"RESULT ts={stamp} checked={checked} green={green} red={len(reds)} rc={rc}")
    for schedule_id, name, missing in reds:
        print(f"RED id={schedule_id} name={name} missing={missing}")
    if notify:
        ports.alert(stamp=stamp, checked=checked, reds=reds, tool_error=None)
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


def cmd_schedules_verify(
    *, check_file: str | None = None, notify: bool = True, ports: VerifyPorts | None = None
) -> int:
    """`ava schedules verify [--check-file PATH] [--no-notify]` — the dry-import and call-signature sweep.

    Read-only one-shot check of every in-store script (stopped rows included):
    py_compile, a top-level-imports-only execution in this checkout's runner
    venv, and a `inspect.signature().bind` of every call the script makes into
    repo code — nothing is started, stopped, or written. Catches the drift
    classes where a repo module move (task #4800: #2678, the 2026-09-25 R3
    Wave-2 regression) or a changed signature (2026-10-03: `catch_up()` gained
    a required `db`) leaves a DB-embedded script stale and the next (re)start
    crash-loops. Line contract: one `RESULT ts=... checked=... green=... red=...
    rc=...` line, one `RED id=... name=... missing=...` per red, `TOOL-ERROR
    ...` on rc=2; exit codes 0 all clean / 1 red / 2 tool error. `--check-file`
    checks one file off-DB (the falsification hook); a non-clean sweep alerts
    through `/api/alerts` unless `--no-notify`."""
    if check_file is not None:
        return _verify_file(check_file)
    ports = ports or VerifyPorts(
        read_rows=_read_schedule_rows, check_script=_check_script, alert=_alert_verify
    )
    return _verify_sweep(notify=notify, ports=ports)


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
    open_starts_at: Callable[[], str | None] = _open_verify_starts_at,
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
        starts_at = open_starts_at()
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


def h_schedules_verify(args: argparse.Namespace) -> int:
    return cmd_schedules_verify(check_file=args.check_file, notify=not args.no_notify)
