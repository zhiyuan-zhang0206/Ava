#!/usr/bin/env python3
"""Out-of-band outage triage: read-only diagnosis of a host's maintenance hold.

When the cluster cannot reach a host, the out-of-band probe -- a launchd-driven
watcher on a surviving machine (task #3608) -- raises the alert; this tool is
the diagnosis that alert carries. Over ssh it reads the host's local
maintenance hold, classifies what it finds, and renders the one official
recovery command for the hold's phase (ws #1818 conventions draft, part 2,
2026-09-17). It reads only: it never executes anything on the host, never
releases a hold, and never sends the alert itself.

Two rules shape the output:

- Missing evidence is not a release license (spec part 2 v1.1.2, adopted from
  the stranded-hold verdict). Only an unambiguous ``driver=dead`` reading may
  classify ``orphaned``; ``missing`` / ``unreadable`` shepherd readings, an
  invalid journal, and reader failure all classify ``undetermined``.
- Remote content never reaches the alert verbatim (spec part 2 v1.1.1, the
  desensitization whitelist). Every value this process emits is a whitelisted
  structured field; free text such as the holder string or process argv is
  dropped, and ``--host`` is pattern-validated before it is spliced into the
  rendered command.

Classifications (advisory only -- nothing here acts):

- ``no-hold``: no active hold stands (``inactive`` or a completed ``resumed``
  ledger). Nothing to recover.
- ``orphaned``: an active hold whose recorded shepherd is dead and which
  carries no failed receipts -- the state a stop-class ladder leaves when its
  executor dies before the start leg (the 2026-09-17 blackout, task #3719).
- ``owned``: an active hold whose recorded shepherd is alive; something is
  still driving it.
- ``stranded``: an active hold with failed receipts -- the repair path, not
  ``resume --cancel``.
- ``undetermined``: evidence missing or unreadable; the alert degrades to the
  plain copy plus ``hold=undetermined``.

The recovery-command table is deliberately decoupled from the classification
(spec part 2, D-addendum): a readable phase always renders its command,
whatever the diagnosis concluded; an uncovered or missing phase renders none.

Exit status: 0 once a triage was produced -- a failed read is data
(``reachable: false``), not a process error; 2 is reserved for usage errors.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import json
import re
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, cast

# Read bounds (spec part 2: bounded reads; the probe's alert budget). The
# connect bound is the spec's; the hard per-read bound leaves the whole triage
# well inside the alert path. Flags expose both so a deployment can adjust
# them without a code change.
_DEFAULT_CONNECT_TIMEOUT_S = 5.0
_DEFAULT_COMMAND_TIMEOUT_S = 10.0

# The two bounded reads, primary then fallback (spec part 2 v1.1.1 B2): the
# status verb is the only source carrying the shepherd's liveness; the raw
# journal is the degraded read that still shows phase and state.
_STATUS_COMMAND = 'cd "$HOME/.ava/source" && .venv/bin/ava maintenance status'
_JOURNAL_COMMAND = 'cat "$HOME/.ava/run/deploy-pause-owner.json"'

# The official recovery table (spec part 2 v1.1.1 B4), one row per phase class:
# a pre-stop hold is cancelled back to serving, a stop-class hold is restarted
# end-to-end. _RESUME_PHASES mirrors ops.strand_hold.PRE_STOP_PHASES.
_RESUME_PHASES = ("preparing", "draining", "drained")
_START_PHASES = ("stopping", "stopped", "starting")

# Vocabulary mirrors shared.pause_owner / shared.hold_driver /
# shared.maintenance_state; an unknown value is a structural surprise and the
# caller falls through to the next source.
_PHASES = ("preparing", "draining", "drained", "stopping", "stopped", "starting", "ready")
_STATES = ("paused", "resumed", "legacy-resumed")
_LIVENESS = ("alive", "dead", "missing", "unreadable")

# The desensitization whitelist (spec part 2 v1.1.1 B5): only strings matching
# these shapes may leave the process; a value that fails renders as `?`. First
# characters exclude `-` throughout: a value reaching an argv tail
# (`--operation <OP>`, `--acquired-at <TS>`, `ssh <host>`) must never read as
# an option.
_OPERATION_PATTERN = re.compile(r"[A-Za-z0-9:._][A-Za-z0-9:._-]{0,119}")
_TIMESTAMP_PATTERN = re.compile(r"[0-9][0-9T:.+Z-]{0,39}")
_HOST_PATTERN = re.compile(r"[A-Za-z0-9._@][A-Za-z0-9._@-]{0,252}")

Classification = Literal["no-hold", "orphaned", "owned", "stranded", "undetermined"]
HoldReading = Literal["present", "absent", "invalid", "unknown"]
Source = Literal["status", "journal", "none"]
Payload = dict[str, object]
Reader = Callable[[str, str], "Payload | None"]


@dataclass(frozen=True)
class Triage:
    """One complete triage reading -- the JSON contract callers consume."""

    host: str
    source: Source
    reachable: bool
    hold: HoldReading
    state: str | None
    phase: str | None
    driver: str | None
    failures: int | None
    operation: str | None
    acquired_at: str | None
    age_seconds: int | None
    classification: Classification
    line: str
    command: str | None


def _mapping(value: object, where: str) -> Payload:
    if not isinstance(value, dict):
        raise TypeError(f"{where} must be a string-keyed object")
    if not all(isinstance(key, str) for key in cast("dict[object, object]", value)):
        raise TypeError(f"{where} must be a string-keyed object")
    return cast("Payload", value)


def _required_string(mapping: Payload, key: str, where: str) -> str:
    value = mapping[key]
    if not isinstance(value, str):
        raise TypeError(f"{where}.{key} must be a string")
    return value


def _phase(value: object) -> str | None:
    """A maintenance phase, or None when the hold records none (a plain pause)."""
    if value is None:
        return None
    if not isinstance(value, str) or value not in _PHASES:
        raise ValueError(f"unknown maintenance phase: {value!r}")
    return value


def _liveness(value: object) -> str:
    if not isinstance(value, str) or value not in _LIVENESS:
        raise ValueError(f"unknown shepherd liveness: {value!r}")
    return value


def _failure_count(value: object, reaped: object = None) -> int:
    """Mirror MaintenanceHold.unsettled_failures on the raw journal shape."""
    if value is None:
        return 0
    if not isinstance(value, dict):
        raise TypeError("maintenance.failures must be an object")
    failures = cast("dict[object, object]", value)
    if reaped is None:
        return len(failures)
    if not isinstance(reaped, dict):
        raise TypeError("maintenance.reaped must be an object")
    certified = cast("dict[object, object]", reaped)
    return sum(agent not in certified for agent in failures)


def _whitelisted(value: object, pattern: re.Pattern[str]) -> str | None:
    """The value when it passes its whitelist pattern, else None (`?` at render)."""
    if isinstance(value, str) and pattern.fullmatch(value):
        return value
    return None


def _validated_host(host: str) -> str:
    """The ssh target unchanged, or ValueError (a leading `-` reads as an ssh option)."""
    if not _HOST_PATTERN.fullmatch(host):
        raise ValueError(f"invalid ssh host: {host!r}")
    return host


def _age_seconds(acquired_at: str | None, now: float) -> int | None:
    if acquired_at is None:
        return None
    try:
        acquired = dt.datetime.fromisoformat(acquired_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    if acquired.tzinfo is None:
        return None
    return max(0, int(now - acquired.timestamp()))


def render_command(
    host: str, phase: str | None, operation: str | None, acquired_at: str | None
) -> str | None:
    """The one official recovery command for a phase (spec part 2 v1.1.1 B4).

    Two rows only: a pre-stop hold resumes ``--cancel``; a stop-class hold is
    restarted end-to-end. An uncovered or unknown phase renders none. A value
    that failed the whitelist renders as ``?`` -- visibly incomplete, never
    remote free text. An invalid host renders none too (defense in depth;
    `triage` validates earlier).
    """
    if not _HOST_PATTERN.fullmatch(host):
        return None
    if phase in _RESUME_PHASES:
        return (
            f"ssh {host} 'cd ~/.ava/source && .venv/bin/ava maintenance resume"
            f" --operation {operation or '?'} --acquired-at {acquired_at or '?'} --cancel'"
        )
    if phase in _START_PHASES:
        return f"ssh {host} 'cd ~/.ava/source && .venv/bin/ava start'"
    return None


def _absent(*, host: str, source: Source) -> Triage:
    """No active hold stands: ``inactive`` or a completed ``resumed`` ledger."""
    return Triage(
        host=host,
        source=source,
        reachable=True,
        hold="absent",
        state=None,
        phase=None,
        driver=None,
        failures=None,
        operation=None,
        acquired_at=None,
        age_seconds=None,
        classification="no-hold",
        line="hold=absent",
        command=None,
    )


def classify_unreadable(*, host: str) -> Triage:
    """Both reads failed: the degraded line (spec part 2 v1.1.1 B2).

    The alert still carries that triage was attempted and could not read the
    host -- silence would erase the attempt.
    """
    return Triage(
        host=host,
        source="none",
        reachable=False,
        hold="unknown",
        state=None,
        phase=None,
        driver=None,
        failures=None,
        operation=None,
        acquired_at=None,
        age_seconds=None,
        classification="undetermined",
        line="hold=undetermined (read failed)",
        command=None,
    )


def classify_status(payload: Payload, *, host: str, now: float | None = None) -> Triage:
    """Classify one `ava maintenance status` reading (the primary source).

    The status payload is the only source carrying the shepherd's liveness
    (`driver.liveness`, judged by shared.hold_driver), so it is the only source
    that can classify beyond `undetermined`. A structural surprise raises and
    the caller falls back to the journal read.
    """
    now = time.time() if now is None else now
    status = payload["status"]
    if not isinstance(status, str) or status not in (*_STATES, "inactive", "invalid"):
        raise ValueError(f"unknown pause-owner status: {status!r}")
    if status == "invalid":
        return Triage(
            host=host,
            source="status",
            reachable=True,
            hold="invalid",
            state=None,
            phase=None,
            driver=None,
            failures=None,
            operation=None,
            acquired_at=None,
            age_seconds=None,
            classification="undetermined",
            line="hold=invalid",
            command=None,
        )
    if status != "paused":
        return _absent(host=host, source="status")
    maintenance = payload["maintenance"]
    phase: str | None = None
    failures = 0
    if maintenance is not None:
        hold = _mapping(maintenance, "maintenance")
        phase = _phase(hold["phase"])
        failures = _failure_count(hold["failures"], hold.get("reaped"))
    driver_value = payload["driver"]
    driver = (
        "missing"
        if driver_value is None
        else _liveness(_mapping(driver_value, "driver")["liveness"])
    )
    operation = _whitelisted(payload["operation"], _OPERATION_PATTERN)
    acquired_at = _whitelisted(payload["acquired_at"], _TIMESTAMP_PATTERN)
    # The judgments mirror the controller's stranded-hold verdict
    # (ops/controllers/stranded_pause.py, `stranded_hold_verdict`): failed
    # receipts read `stranded` (never releasable), a dead shepherd with none
    # reads `orphaned`, and a missing/unreadable shepherd is never a release
    # license (shared/hold_driver.py: `missing` is definitional, only `dead`
    # is the unambiguous birth-checked absence) -- so both stay `undetermined`.
    if failures > 0:
        classification: Classification = "stranded"
    elif driver == "dead":
        classification = "orphaned"
    elif driver == "alive":
        classification = "owned"
    else:
        classification = "undetermined"
    return Triage(
        host=host,
        source="status",
        reachable=True,
        hold="present",
        state=status,
        phase=phase,
        driver=driver,
        failures=failures,
        operation=operation,
        acquired_at=acquired_at,
        age_seconds=_age_seconds(acquired_at, now),
        classification=classification,
        line=f"hold=present phase={phase or '?'} driver={driver} state={status}",
        command=render_command(host, phase, operation, acquired_at),
    )


def classify_journal(payload: Payload, *, host: str, now: float | None = None) -> Triage:
    """Classify the raw deploy-pause-owner.json (the fallback source).

    The journal carries no liveness, so every active reading here classifies
    `undetermined` (spec part 2 v1.1.2, decision 5) and the shepherd displays
    as `?`; phase, state, and the whitelisted command inputs are still read.
    """
    now = time.time() if now is None else now
    state = _required_string(payload, "state", "journal")
    if state not in _STATES:
        raise ValueError(f"unknown pause-owner state: {state!r}")
    if state != "paused":
        return _absent(host=host, source="journal")
    maintenance = payload.get("maintenance")
    phase: str | None = None
    failures = 0
    if maintenance is not None:
        hold = _mapping(maintenance, "maintenance")
        phase = _phase(hold["phase"])
        failures = _failure_count(hold["failures"], hold.get("reaped"))
    operation = _whitelisted(payload.get("holder"), _OPERATION_PATTERN)
    acquired_at = _whitelisted(payload.get("acquired_at"), _TIMESTAMP_PATTERN)
    return Triage(
        host=host,
        source="journal",
        reachable=True,
        hold="present",
        state=state,
        phase=phase,
        driver=None,
        failures=failures,
        operation=operation,
        acquired_at=acquired_at,
        age_seconds=_age_seconds(acquired_at, now),
        classification="undetermined",
        line=f"hold=present phase={phase or '?'} driver=? state={state}",
        command=render_command(host, phase, operation, acquired_at),
    )


def _read_source(
    host: str, remote_command: str, *, connect_timeout_s: float, command_timeout_s: float
) -> Payload | None:
    """One bounded ssh read; None on any failure to produce a JSON object.

    BatchMode stops a prompt from ever hanging the alert path; the subprocess
    timeout is the hard bound. A non-zero exit (unreachable host, missing file,
    absent CLI) and non-JSON or non-object stdout all read as no evidence.
    """
    argv = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        f"ConnectTimeout={connect_timeout_s:g}",
        host,
        remote_command,
    ]
    try:
        proc = subprocess.run(  # noqa: S603 -- fixed argv; --host is pattern-validated
            argv,
            capture_output=True,
            text=True,
            timeout=command_timeout_s,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    try:
        payload: object = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    return cast("Payload", payload)


def _make_reader(connect_timeout_s: float, command_timeout_s: float) -> Reader:
    def read(host: str, remote_command: str) -> Payload | None:
        return _read_source(
            host,
            remote_command,
            connect_timeout_s=connect_timeout_s,
            command_timeout_s=command_timeout_s,
        )

    return read


def triage(
    host: str,
    *,
    now: float | None = None,
    read: Reader | None = None,
    connect_timeout_s: float = _DEFAULT_CONNECT_TIMEOUT_S,
    command_timeout_s: float = _DEFAULT_COMMAND_TIMEOUT_S,
) -> Triage:
    """Read the two sources within bounds and classify (the degradation matrix).

    Primary read is `ava maintenance status`; when it fails or surprises, the
    raw journal is read; when that fails too, the degraded line. A failed read
    never blocks producing a triage; an invalid host raises ValueError before
    any read.
    """
    host = _validated_host(host)
    reader = _make_reader(connect_timeout_s, command_timeout_s) if read is None else read
    status_payload = reader(host, _STATUS_COMMAND)
    if status_payload is not None:
        with contextlib.suppress(KeyError, TypeError, ValueError):
            return classify_status(status_payload, host=host, now=now)
    journal_payload = reader(host, _JOURNAL_COMMAND)
    if journal_payload is not None:
        with contextlib.suppress(KeyError, TypeError, ValueError):
            return classify_journal(journal_payload, host=host, now=now)
    return classify_unreadable(host=host)


def triage_json(result: Triage) -> Payload:
    """The triage as the one JSON object callers parse."""
    return {
        "host": result.host,
        "source": result.source,
        "reachable": result.reachable,
        "hold": result.hold,
        "state": result.state,
        "phase": result.phase,
        "driver": result.driver,
        "failures": result.failures,
        "operation": result.operation,
        "acquired_at": result.acquired_at,
        "age_seconds": result.age_seconds,
        "classification": result.classification,
        "line": result.line,
        "command": result.command,
    }


def render_text(result: Triage) -> str:
    """The operator-facing report: identity line, diagnosis line, command."""
    lines = [
        f"oob-triage host={result.host} source={result.source}"
        f" classification={result.classification}",
        f"  {result.line}",
    ]
    if result.command is not None:
        lines.append(f"  command: {result.command}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only outage triage of a host's maintenance hold."
    )
    parser.add_argument("--host", required=True, help="ssh target alias of the unreachable host")
    parser.add_argument("--json", action="store_true", help="emit the triage as one JSON object")
    parser.add_argument(
        "--connect-timeout",
        type=float,
        default=_DEFAULT_CONNECT_TIMEOUT_S,
        metavar="SECONDS",
        help="ssh connect bound per read (default: %(default)s)",
    )
    parser.add_argument(
        "--command-timeout",
        type=float,
        default=_DEFAULT_COMMAND_TIMEOUT_S,
        metavar="SECONDS",
        help="hard bound per read (default: %(default)s)",
    )
    args = parser.parse_args(argv)
    try:
        host = _validated_host(cast(str, args.host))
    except ValueError as error:
        parser.error(str(error))
    result = triage(
        host,
        connect_timeout_s=cast(float, args.connect_timeout),
        command_timeout_s=cast(float, args.command_timeout),
    )
    if cast(bool, args.json):
        print(json.dumps(triage_json(result), sort_keys=True))
    else:
        print(render_text(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
