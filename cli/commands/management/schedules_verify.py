"""`ava schedules verify` — the dry-import, undefined-name and call-signature sweep over every
in-store schedule script."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import Any

from base import telemetry

# One child's budget. task #3696 exception inventory: a self-imposed guard on a
# single check child — a healthy top-level import set takes seconds, so a child
# still running at 90s is wedged and must not park the sweep behind it.
_VERIFY_CHILD_TIMEOUT_S = 90.0


# The child's last stdout line -> the RED detail (prefix added to the line's remainder).
_CHILD_VERDICTS = (
    ("CHILD-COMPILE-ERROR:", "compile-error:"),
    ("CHILD-MODULE:", ""),
    ("CHILD-EXC:", ""),
    ("CHILD-UNDEF:", "undefined-name:"),
    ("CHILD-SIG:", "call-signature:"),
)


def _check_script(script: str) -> str | None:
    """Dry-import one in-store script, check its name reads and bind its repo-API call sites;
    None = clean, else the RED detail (`<module>` / `compile-error:<l>:<m>` / `<Exc>:<msg>` /
    `undefined-name:<l> <name>` / `call-signature:<l> <call>: <why>`).

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


def _read_rows_file(path: str) -> list[tuple[int, str, str]]:
    """The rows `_read_schedule_rows` returned, dumped as a JSON list of `[id, name, script]`."""
    data: Any = json.loads(Path(path).read_text(encoding="utf-8"))
    return [(int(row[0]), str(row[1]), str(row[2])) for row in data]


@dataclass(frozen=True)
class VerifyPorts:
    """What a verify sweep reads and does: the schedule rows, the per-script dry
    import and the failure report. `cmd_schedules_verify` wires the real ones."""

    read_rows: Callable[[], list[tuple[int, str, str]]]
    check_script: Callable[[str], str | None]
    report: Callable[..., None]


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
            ports.report(checked=0, reds=[], tool_error=detail)
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
    if notify and reds:
        ports.report(checked=checked, reds=reds, tool_error=None)
    return rc


def _verify_file(path: str) -> int:
    """`--check-file PATH` — dry-import one script file (no DB, no signal)."""
    try:
        source = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        print(f"cannot read script file: {exc}", file=sys.stderr)
        return 2
    missing = _check_script(source)
    print("CHECK-OK" if missing is None else f"CHECK-RED missing={missing}")
    return 0 if missing is None else 1


def cmd_schedules_verify(
    *,
    check_file: str | None = None,
    rows_file: str | None = None,
    notify: bool = True,
    ports: VerifyPorts | None = None,
) -> int:
    """`ava schedules verify [--check-file PATH | --rows-file PATH] [--no-notify]` — the dry-import and call-signature sweep.

    Read-only one-shot check of every in-store script (stopped rows included):
    py_compile, a top-level-imports-only execution in this checkout's runner
    venv, a static read of every name the script uses (any scope) against the
    names it binds, and a `inspect.signature().bind` of every call the script
    makes into repo code — nothing is started, stopped, or written. Catches the
    drift classes where a repo module move (task #4800: #2678, the 2026-09-25 R3
    Wave-2 regression), a rename leaving a top-level statement reading a name
    nothing imports (2026-10-01 audit: `base.__file__` after `shared` -> `base`),
    or a changed signature (2026-10-03: `catch_up()` gained a required `db`)
    leaves a DB-embedded script stale and the next (re)start crash-loops.
    Line contract: one `RESULT ts=... checked=... green=... red=...
    rc=...` line, one `RED id=... name=... missing=...` per red, `TOOL-ERROR
    ...` on rc=2; exit codes 0 all clean / 1 red / 2 tool error. `--check-file`
    checks one file off-DB (the falsification hook); `--rows-file` sweeps a JSON
    dump of the table (`[[id, name, script], ...]`) instead of reading it, so
    this checkout never dials the database, and never signals (the signal
    reports the live table); otherwise a non-clean sweep emits a
    `schedule_verify_failed` event unless `--no-notify`."""
    if check_file is not None:
        return _verify_file(check_file)
    if rows_file is not None:
        notify = False
    ports = ports or VerifyPorts(
        read_rows=partial(_read_rows_file, rows_file) if rows_file else _read_schedule_rows,
        check_script=_check_script,
        report=_report_verify,
    )
    return _verify_sweep(notify=notify, ports=ports)


def _report_verify(
    *, checked: int, reds: list[tuple[int, str, str]], tool_error: str | None
) -> None:
    """Emit one `schedule_verify_failed` event for a non-clean sweep.

    The event is the signal a Grafana rule reads; a clean sweep emits nothing,
    so the alert resolves on its own once reds stop recurring."""
    if tool_error is not None:
        detail = f"tool-error: {tool_error}"
    else:
        listing = "; ".join(
            f"id={sid} name={name} missing={missing}" for sid, name, missing in reds
        )
        detail = f"{len(reds)}/{checked} in-store schedule script(s) failed dry-import: {listing}"
    telemetry.emit(
        "telemetry",
        "schedule_verify_failed",
        level="error",
        source="schedule-verify",
        attributes={
            "checked": checked,
            "red": len(reds),
            "tool_error": tool_error,
            "detail": detail,
        },
    )


def h_schedules_verify(args: argparse.Namespace) -> int:
    return cmd_schedules_verify(
        check_file=args.check_file, rows_file=args.rows_file, notify=not args.no_notify
    )
