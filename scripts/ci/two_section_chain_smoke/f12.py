"""F12/F12b sampling helpers: probe rounds, tccd window parsing, attribution joins."""

from __future__ import annotations

import datetime
import json
import re
import time
from pathlib import Path
from typing import Any

from .support import SmokeError, _fail, _pid_alive, _run, _save, _wait_for

_F12_ROUND_WAIT_S = 10.0

_F12_REQUESTING_RE = re.compile(
    r"requesting=\{TCCDProcess: identifier=(?P<requesting_id>[^,]*), pid=(?P<requesting_pid>\d+)"
)
_F12_RESPONSIBLE_RE = re.compile(
    r"responsible=\{TCCDProcess: identifier=(?P<responsible_id>[^,]*), pid=(?P<responsible_pid>\d+)"
)
_LOG_STAMP_RE = re.compile(r"^(?P<stamp>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3})")


def _f12_chain_line(pid: int) -> str:
    proc = _run(["ps", "-o", "pid=,ppid=,pgid=,lstart=,command=", "-p", str(pid)], check=False)
    return proc.stdout.strip() or f"{pid} <gone>"


def _f12_round(workdir: Path, unit_id: str, tag: str, pid: int, timeout: float) -> dict[str, Any]:
    rounds_path = workdir / f"unit-results-{unit_id}.json.rounds"

    def _found():
        if not rounds_path.exists():
            return None
        for line in reversed(rounds_path.read_text().splitlines()):
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if record.get("round") == tag and record.get("pid") == pid:
                return record
        return None

    return _wait_for(f"round {tag} from unit {unit_id} pid {pid}", _found, timeout, "f12")


def _f12_point(
    workdir: Path, tag: str, units: list[tuple[str, int]], pids: list[int]
) -> dict[str, Any]:
    """Trigger one probe round per unit id; collect per-pid replies; snapshot chains.

    A unit that is already dead gets an `error` marker without waiting; a live
    unit that never answers times out into `error: no probe response`. The join
    skips error rows; the summary counts them as missing rounds.
    """
    smoke_ts = round(time.time(), 3)
    for unit_id in sorted({unit_id for unit_id, _ in units}):
        (workdir / f"unit-results-{unit_id}.json.req").write_text(tag)
    rows = []
    for unit_id, pid in units:
        if not _pid_alive(pid):
            record: dict[str, Any] = {"round": tag, "pid": pid, "error": "unit dead"}
        else:
            try:
                record = _f12_round(workdir, unit_id, tag, pid, _F12_ROUND_WAIT_S)
            except SmokeError:
                record = {"round": tag, "pid": pid, "error": "no probe response"}
        record["unit"] = unit_id
        rows.append(record)
    return {"smoke_ts": smoke_ts, "rows": rows, "chains": [_f12_chain_line(pid) for pid in pids]}


def _f12_parse_window(log_text: str) -> dict[int, list[tuple[float, str]]]:
    """Index AUTHREQ_ATTRIBUTION lines by requesting pid: pid -> [(ts, line)]."""
    lines: dict[int, list[tuple[float, str]]] = {}
    for line in log_text.splitlines():
        stamp = _LOG_STAMP_RE.match(line)
        requesting = _F12_REQUESTING_RE.search(line)
        if stamp is None or requesting is None:
            continue
        ts = (
            datetime.datetime.strptime(stamp.group("stamp"), "%Y-%m-%d %H:%M:%S.%f")
            .astimezone()
            .timestamp()
        )
        lines.setdefault(int(requesting.group("requesting_pid")), []).append((ts, line))
    return lines


def _f12_join(
    samples: dict[str, Any], lines: dict[int, list[tuple[float, str]]]
) -> list[dict[str, Any]]:
    """Join probe rounds against the parsed window: first 3 requests at/after ts - 0.15s."""
    joined = []
    for point in samples["points"].values():
        for record in point["rows"]:
            if not isinstance(record.get("ts"), (int, float)):
                continue
            requests = []
            for ts, line in lines.get(int(record["pid"]), []):
                if ts < record["ts"] - 0.15:
                    continue
                responsible = _F12_RESPONSIBLE_RE.search(line)
                requests.append(
                    {
                        "line_ts": ts,
                        "responsible_id": responsible.group("responsible_id")
                        if responsible
                        else None,
                        "responsible_pid": int(responsible.group("responsible_pid"))
                        if responsible
                        else None,
                    }
                )
                if len(requests) >= 3:
                    break
            joined.append(
                {
                    "unit": record["unit"],
                    "point": record["round"],
                    "pid": record["pid"],
                    "requests": requests,
                }
            )
    return joined


def _f12_attribution_summary(samples: dict[str, Any]) -> dict[str, Any]:
    """Aggregate sampled rounds: per-point + total requests/unattributed/missing."""
    blank = {"requests": 0, "attributed": 0, "unattributed": 0, "missing_rounds": 0}
    by_point: dict[str, dict[str, int]] = {}
    for point in samples["points"].values():
        for record in point["rows"]:
            counts = by_point.setdefault(record["round"], dict(blank))
            if not isinstance(record.get("ts"), (int, float)):
                counts["missing_rounds"] += 1
    for row in samples["tccd"]:
        counts = by_point.setdefault(row["point"], dict(blank))
        for request in row["requests"]:
            counts["requests"] += 1
            if request["responsible_pid"] is None:
                counts["unattributed"] += 1
            else:
                counts["attributed"] += 1
    totals = {key: sum(counts[key] for counts in by_point.values()) for key in blank}
    return {"totals": totals, "by_point": by_point}


def _f12_expect_attribution(
    samples: dict[str, Any], expectations: list[tuple[str, list[int] | None, int]]
) -> list[str]:
    """Check sampled requests resolve to the expected responsible pid.

    Each expectation is (point, pids, want_pid): at `point`, every request of
    every joined row whose pid is listed (None = all joined rows) must carry a
    responsible pid equal to `want_pid`, and each listed pid must have a joined
    row. Points absent from `expectations` are pure observations. Returns one
    human-readable violation string per failed check.
    """
    rows_by_point: dict[str, list[dict[str, Any]]] = {}
    for row in samples["tccd"]:
        rows_by_point.setdefault(row["point"], []).append(row)
    violations: list[str] = []
    for point, pids, want_pid in expectations:
        joined = rows_by_point.get(point, [])
        if pids is None:
            selected = joined
            if not selected:
                violations.append(f"{point}: no sampled rows")
                continue
        else:
            wanted = set(pids)
            selected = [row for row in joined if row["pid"] in wanted]
            seen = {row["pid"] for row in selected}
            for pid in sorted(wanted - seen):
                violations.append(f"{point}: no sampled row for pid {pid} (dead or no response)")
        for row in selected:
            if not row["requests"]:
                violations.append(f"{point} {row['unit']} pid {row['pid']}: no requests observed")
                continue
            for request in row["requests"]:
                if request["responsible_pid"] is None:
                    violations.append(
                        f"{point} {row['unit']} pid {row['pid']}: request has no responsible"
                    )
                elif request["responsible_pid"] != want_pid:
                    violations.append(
                        f"{point} {row['unit']} pid {row['pid']}: responsible pid "
                        f"{request['responsible_pid']} != expected {want_pid}"
                    )
    return violations


def _f12_summary_text(label: str, samples: dict[str, Any]) -> str:
    """One log line with the aggregate request counts for a sampling phase."""
    totals = samples["summary"]["totals"]
    return (
        f"{label}: {totals['requests']} requests, {totals['unattributed']} unattributed, "
        f"{totals['missing_rounds']} missing rounds"
    )


def _f12_finish(
    evidence: Path,
    samples: dict[str, Any],
    label: str,
    expectations: list[tuple[str, list[int] | None, int]],
) -> None:
    """Join the tccd window, save the sampling record, fail on attribution violations."""
    _f12_join_tccd(evidence, samples, name=f"{label}-tccd-window.txt")
    print(_f12_summary_text(label, samples))
    violations = _f12_expect_attribution(samples, expectations)
    if violations:
        samples["violations"] = violations
    _save(evidence, f"{label}-sampling.json", json.dumps(samples, indent=2))
    if violations:
        _fail(label, f"attribution violations: {'; '.join(violations)}")


def _f12_join_tccd(
    evidence: Path, samples: dict[str, Any], *, name: str = "f12-tccd-window.txt"
) -> None:
    """Join sampled probe rounds against the tccd AUTHREQ_ATTRIBUTION log window.

    The only IO on the F12b attribution path: pulls the log window, saves it as
    `name` under `evidence`, then joins + summarizes (pure helpers above) into
    `samples`.
    """
    first = min(point["smoke_ts"] for point in samples["points"].values())
    span = max(2, int((time.time() - first) / 60) + 2)
    log_text = _run(
        [
            "/usr/bin/log",
            "show",
            "--last",
            f"{span}m",
            "--style",
            "compact",
            "--predicate",
            'eventMessage CONTAINS "AUTHREQ_ATTRIBUTION"',
        ],
        timeout=120.0,
    ).stdout
    _save(evidence, name, log_text)
    samples["tccd"] = _f12_join(samples, _f12_parse_window(log_text))
    samples["summary"] = _f12_attribution_summary(samples)
