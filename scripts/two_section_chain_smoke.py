#!/usr/bin/env python
"""Two-section chain smoke: launchd -> permissions-helper -> ava-root -> unit.

The dev-side acceptance run for the macOS "two-section" adapter (task #3209,
design #3195). A helper compiled from this checkout is registered as a
throwaway launchd job under an isolated workdir; the helper seeds a dev
ava-root from a seed config (the K3 face), and this script verifies the whole
chain plus the keeper's crash semantics:

  build      compile + ad-hoc sign a dev helper from this checkout
  launch     bootstrap the throwaway launchd job, wait for the helper
  chain      launchd -> helper -> root -> unit parentage via ps + root status
  attribute  a unit's TCCAccessPreflight requests resolve to the helper (F11)
  conflict   kill -9 the helper: launchd relaunches it; the relaunched helper
             finds the orphan root and rests in `conflict` (no double-spawn,
             no signal to the foreign PID); `root_stop` is refused with or
             without force; native recovery through the orphan's own
             `shutdown` verb closes its tree, and the keeper seeds a fresh
             root on the freed run dir as the relaunched helper's child; with
             --sample-conflict the phase also samples the whole tree chain +
             TCC attribution across the helper death/replacement window
             (F12b, task #3380)
  restart    kill -9 the root: its units keep running (the design's "lose
             attribution, not service"); the keeper relaunches root, and the
             replacement refuses cold start while the killed generation's
             service custody is unresolved, so no duplicate tree is born; with
             --sample-restart the phase also samples the surviving units'
             chain + TCC attribution around the crash (F12, task #3377)

The helper binds its home to the seed file's directory, so the seed lives in
the root run dir. Nothing here touches production: the binary is
throwaway-signed, every path lives under the workdir, and the launchd job uses
its own test label (never the production helper's). The helper's first-run
registration nudge is disabled via AVA_PERMISSIONS_HELPER_SKIP_REGISTRATION=1
-- an Aqua-session helper with a fresh code identity would otherwise raise TCC
dialogs on a machine nobody is sitting at.

Exit 0 = every phase passed. Evidence (ps/logs/status snapshots) is retained
under the workdir; pass --cleanup to remove it. Run with the repository venv,
on macOS, from a checkout that contains services/ava_root (dev/CI only).
"""

from __future__ import annotations

import argparse
import contextlib
import datetime
import json
import os
import plistlib
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, NoReturn, cast

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LABEL = "com.ava.test.two-section-chain-smoke"
BUNDLE_ID = "com.ava.permissions-helper"

_ROOT_SOCKET = "ava-root.sock"
_HELPER_WAIT_S = 30.0
_ROOT_WAIT_S = 30.0
_RESTART_WAIT_S = 20.0
_POLL_S = 0.25
_F12_STABLE_S = 6.0
_F12_ROUND_WAIT_S = 10.0
_UNIT_IDS = ("heartbeat", "heartbeat-b", "heartbeat-c")

_ATTRIBUTION_RE = re.compile(
    r"responsible=\{TCCDProcess: identifier=(?P<responsible_id>[^,]*), pid=(?P<responsible_pid>\d+)"
    r".*?(?P<role>requesting|accessing)=\{TCCDProcess: identifier=(?P<requesting_id>[^,]*), "
    r"pid=(?P<requesting_pid>\d+)"
)

_F12_REQUESTING_RE = re.compile(
    r"requesting=\{TCCDProcess: identifier=(?P<requesting_id>[^,]*), pid=(?P<requesting_pid>\d+)"
)
_F12_RESPONSIBLE_RE = re.compile(
    r"responsible=\{TCCDProcess: identifier=(?P<responsible_id>[^,]*), pid=(?P<responsible_pid>\d+)"
)
_LOG_STAMP_RE = re.compile(r"^(?P<stamp>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3})")

_UNIT_PROBE = '''\
\
"""Unit probe: side-effect-free preflight queries + heartbeat; answers sampled rounds.

TCCAccessPreflight never prompts and never touches protected data; tccd still
records every call with this process's attribution, which the smoke reads back
to prove the launchd -> helper -> root -> unit chain resolves to the helper.
"""

import ctypes
import json
import os
import sys
import time

SERVICES = [
    "kTCCServiceSystemPolicyDesktopFolder",
    "kTCCServiceSystemPolicyAllFiles",
    "kTCCServiceScreenCapture",
]

_cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
_cf.CFStringCreateWithCString.restype = ctypes.c_void_p
_cf.CFStringCreateWithCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]
_tcc = ctypes.CDLL("/System/Library/PrivateFrameworks/TCC.framework/Versions/A/TCC")
_tcc.TCCAccessPreflight.restype = ctypes.c_int
_tcc.TCCAccessPreflight.argtypes = [ctypes.c_void_p, ctypes.c_void_p]


def preflight(service):
    cf_service = _cf.CFStringCreateWithCString(None, service.encode(), 0x08000100)
    return _tcc.TCCAccessPreflight(ctypes.c_void_p(cf_service), None)


def main():
    results_path, beat_path = sys.argv[1], sys.argv[2]
    request_path = results_path + ".req"
    rounds_path = results_path + ".rounds"
    results = {}
    for service in SERVICES:
        results[service] = preflight(service)
    with open(results_path, "w") as handle:
        json.dump({"pid": os.getpid(), "ppid": os.getppid(), "services": results}, handle)
    seen = None
    while True:
        with open(beat_path, "a") as handle:
            handle.write("%.0f\\n" % time.time())
        try:
            with open(request_path) as handle:
                current = handle.read().strip()
        except OSError:
            current = None
        if current and current != seen:
            seen = current
            record = {
                "round": current,
                "ts": round(time.time(), 3),
                "pid": os.getpid(),
                "ppid": os.getppid(),
                "pgid": os.getpgid(0),
                "sid": os.getsid(0),
                "services": {service: preflight(service) for service in SERVICES},
            }
            with open(rounds_path, "a") as handle:
                handle.write(json.dumps(record) + "\\n")
        time.sleep(1)


main()
'''


class SmokeError(RuntimeError):
    """One smoke phase failed; the message carries the phase and the detail."""

    def __init__(self, phase: str, detail: str) -> None:
        super().__init__(f"FAIL(phase={phase}): {detail}")
        self.phase = phase
        self.detail = detail


def _fail(phase: str, detail: str) -> NoReturn:
    """Raise one phase failure (kept out of the try bodies for TRY301)."""
    raise SmokeError(phase, detail)


def _run(
    cmd: list[str], *, check: bool = True, timeout: float = 120.0
) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(  # noqa: S603 - fixed argv lists built in this file, no untrusted input
        cmd, capture_output=True, text=True, timeout=timeout, check=False
    )
    if check and proc.returncode != 0:
        tail = (proc.stdout + proc.stderr).strip()[-800:]
        _fail("run", f"{cmd[0]} exited {proc.returncode}: {tail}")
    return proc


def _wait_for(what: str, predicate, timeout: float, phase: str):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(_POLL_S)
    _fail(phase, f"timed out after {timeout:.0f}s waiting for {what}")


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _ps_line(*pids: int) -> str:
    proc = _run(["ps", "-o", "pid=,ppid=,lstart=,command=", "-p", ",".join(str(p) for p in pids)])
    return proc.stdout.strip()


def _save(evidence: Path, name: str, text: str) -> None:
    (evidence / name).write_text(text + "\n")


def _launchctl_domain() -> str:
    return f"gui/{os.getuid()}"


def _bootout(label: str) -> None:
    _run(["launchctl", "bootout", f"{_launchctl_domain()}/{label}"], check=False, timeout=30)


def _job_pid(label: str) -> int | None:
    proc = _run(["launchctl", "list"], check=False, timeout=30)
    for line in proc.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) >= 3 and parts[2].strip() == label:
            pid_field = parts[0].strip()
            return int(pid_field) if pid_field.isdigit() else None
    return None


def _kill(pid: int, sig: int = signal.SIGTERM) -> None:
    with contextlib.suppress(ProcessLookupError):
        os.kill(pid, sig)


def _tail(path: Path, limit: int = 4000) -> str:
    try:
        return path.read_text()[-limit:]
    except OSError:
        return "<missing>"


def _refusal(call: Callable[[], object]) -> str:
    """The error text of a call that must be refused ("" when it was accepted)."""
    try:
        call()
    except Exception as exc:  # the refusal message is the assertion
        return str(exc)
    return ""


def _ping_or_none(helper_client, sock: Path):
    try:
        return helper_client.ping(sock_path=sock)["pong"]
    except Exception:
        return None


def _status_if_running(root_status, *, excluding: int | None = None):
    try:
        status = root_status()
    except Exception:
        return None
    if status["root"]["running"] and isinstance(status["root"]["pid"], int):
        pid = int(status["root"]["pid"])
        if excluding is not None and pid == excluding:
            return None
        return status
    return None


def _status_if_keeper_running(helper_root_status):
    try:
        status = helper_root_status()
    except Exception:
        return None
    if status.get("state") == "running" and isinstance(status.get("pid"), int):
        return status
    return None


def _status_if_conflict(helper_root_status):
    try:
        status = helper_root_status()
    except Exception:
        return None
    return status if status.get("state") == "conflict" else None


def _status_if_refused(helper_root_status, restarts_before: int):
    """The keeper once a replacement root has exited `refused` after the crash."""
    try:
        status = helper_root_status()
    except Exception:
        return None
    refused = status.get("last_exit", {}).get("kind") == "refused"
    return status if refused and int(status["restarts"]) > restarts_before else None


def _probe_pids(probe: Path) -> set[int]:
    """Every live unit-probe process of this workdir, whoever spawned it."""
    return {int(pid) for pid in _run(["pgrep", "-f", str(probe)], check=False).stdout.split()}


def _relaunched_helper(label: str, old_pid: int):
    pid = _job_pid(label)
    if pid is None or pid == old_pid:
        return None
    return pid


def _unit_entry(status: dict, unit_id: str) -> dict:
    for entry in status["units"]:
        if entry["id"] == unit_id and isinstance(entry["pid"], int):
            return entry
    _fail("chain", f"unit {unit_id!r} has no live pid in {json.dumps(status)[:400]}")


def _ppid_of(pid: int) -> int:
    proc = _run(["ps", "-o", "ppid=", "-p", str(pid)])
    return int(proc.stdout.strip())


def _root_call(run_dir: Path, verb: str) -> dict[str, Any]:
    """Send one K1 verb over the raw control socket (stdlib only).

    The smoke drives the root as a black box — process tree, socket, K1
    verbs — so it never depends on the root package's client API.
    """
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(10.0)
        sock.connect(str(run_dir / _ROOT_SOCKET))
        sock.sendall(json.dumps({"verb": verb}).encode() + b"\n")
        line = sock.makefile("rb").readline()
    response = json.loads(line)
    if not response.get("ok"):
        _fail("root", f"{verb} refused: {response}")
    return cast("dict[str, Any]", response["result"])


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


def main() -> int:  # noqa: PLR0915 - one bounded smoke lifecycle: every phase, wait, and teardown live together on purpose
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument(
        "--workdir",
        default="/tmp/two-section-chain-smoke",  # noqa: S108 - scratch evidence dir, never secret
    )
    parser.add_argument("--label", default=DEFAULT_LABEL)
    parser.add_argument("--python", default=sys.executable, help="interpreter for root + units")
    parser.add_argument("--skip-attribution", action="store_true", help="skip the F11 tccd check")
    parser.add_argument(
        "--sample-restart",
        action="store_true",
        help="sample chains + TCC attribution across the root crash restart (F12)",
    )
    parser.add_argument(
        "--sample-conflict",
        action="store_true",
        help="sample chains + TCC attribution across the helper crash conflict phase (F12b)",
    )
    parser.add_argument("--cleanup", action="store_true", help="remove the workdir at the end")
    args = parser.parse_args()

    workdir = Path(args.workdir).expanduser()
    if not workdir.is_absolute():
        print(f"FAIL(phase=args): workdir must be absolute: {workdir}")
        return 2
    if os.getuid() == 0:
        print("FAIL(phase=args): run as the desktop user, not root (launchctl gui domain)")
        return 2
    if not (REPO_ROOT / "services" / "ava_root").is_dir():
        print(
            f"FAIL(phase=args): {REPO_ROOT} does not contain services/ava_root (stack the root slice)"
        )
        return 2

    workdir.mkdir(parents=True, exist_ok=True)
    evidence = workdir / "evidence"
    evidence.mkdir(exist_ok=True)
    helper_sock = workdir / "helper.sock"
    run_dir = workdir / "root-run"
    label = args.label
    recorded_pids: set[int] = set()
    phases: list[str] = []

    from services.permissions_helper import client as helper_client

    def root_status() -> dict[str, Any]:
        return _root_call(run_dir, "status")

    def helper_root_status() -> dict[str, Any]:
        return cast("dict[str, Any]", helper_client.root_status(sock_path=helper_sock))

    def phase_pass(name: str, detail: str) -> None:
        phases.append(name)
        print(f"PHASE {name}: PASS ({detail})")

    try:
        # ---- build ---------------------------------------------------------
        print(f"workdir: {workdir}")
        app = workdir / "app" / "AvaPermissionsHelper.app"
        exe = app / "Contents" / "MacOS" / "AvaPermissionsHelper"
        shutil.rmtree(app, ignore_errors=True)
        exe.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(
            REPO_ROOT / "services" / "permissions_helper" / "helper" / "Info.plist",
            app / "Contents" / "Info.plist",
        )
        _run(
            [
                "swiftc",
                "-O",
                str(REPO_ROOT / "services" / "permissions_helper" / "helper" / "main.swift"),
                "-o",
                str(exe),
            ],
            timeout=300.0,
        )
        exe.chmod(0o755)
        _run(
            ["codesign", "--force", "--sign", "-", "--identifier", BUNDLE_ID, str(app)],
            timeout=120.0,
        )
        _save(
            evidence,
            "build.txt",
            _run(["swiftc", "--version"]).stdout
            + _run(["codesign", "-dvv", str(app)], check=False).stderr,
        )
        phase_pass("build", f"ad-hoc signed helper at {app}")

        # ---- fixtures ------------------------------------------------------
        # Fresh run surface: stale fixtures/logs from an earlier run must not
        # satisfy this run's waits (the attribution split and the custody
        # refusal check read files, not memory), and a stale run dir's custody
        # records or stop intents would refuse this run's root outright.
        shutil.rmtree(run_dir, ignore_errors=True)
        run_dir.mkdir(parents=True)
        for stale in [
            *workdir.glob("unit-*"),
            workdir / "root.stdout.log",
            workdir / "root.stderr.log",
            workdir / "helper.stdout.log",
            workdir / "helper.stderr.log",
        ]:
            stale.unlink(missing_ok=True)
        probe = workdir / "unit_probe.py"
        probe.write_text(_UNIT_PROBE)
        manifests = workdir / "manifests.json"
        manifests.write_text(
            json.dumps(
                {
                    "units": [
                        {
                            "id": unit_id,
                            "exec": [
                                args.python,
                                str(probe),
                                str(workdir / f"unit-results-{unit_id}.json"),
                                str(workdir / f"unit-{unit_id}.beat"),
                            ],
                            "restart": "always",
                            "attach": "root",
                        }
                        for unit_id in _UNIT_IDS
                    ]
                }
            )
        )
        seed = {
            "argv": [
                args.python,
                "-m",
                "services.ava_root",
                "--run-dir",
                str(run_dir),
                "--manifests",
                str(manifests),
            ],
            "cwd": str(REPO_ROOT),
            "run_dir": str(run_dir),
            "stdout": str(workdir / "root.stdout.log"),
            "stderr": str(workdir / "root.stderr.log"),
            "env": {"HOME": str(Path.home()), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
        }
        seed_path = run_dir / "seed.json"
        seed_path.write_text(json.dumps(seed, indent=2))
        plist = {
            "Label": label,
            "ProgramArguments": [str(exe)],
            "EnvironmentVariables": {
                "AVA_PERMISSIONS_HELPER_SOCKET": str(helper_sock),
                "AVA_PERMISSIONS_HELPER_ROOT_SEED": str(seed_path),
                "AVA_PERMISSIONS_HELPER_SKIP_REGISTRATION": "1",
            },
            "RunAtLoad": True,
            "KeepAlive": True,
            "StandardOutPath": str(workdir / "helper.stdout.log"),
            "StandardErrorPath": str(workdir / "helper.stderr.log"),
        }
        plist_path = workdir / f"{label}.plist"
        with plist_path.open("wb") as handle:
            plistlib.dump(plist, handle)

        # ---- launch --------------------------------------------------------
        _bootout(label)
        helper_sock.unlink(missing_ok=True)
        _run(["launchctl", "bootstrap", _launchctl_domain(), str(plist_path)], timeout=60.0)
        _wait_for("helper socket", helper_sock.exists, _HELPER_WAIT_S, "launch")
        ping = _wait_for(
            "helper ping",
            lambda: _ping_or_none(helper_client, helper_sock),
            _HELPER_WAIT_S,
            "launch",
        )
        helper_pid = _wait_for("helper job pid", lambda: _job_pid(label), _HELPER_WAIT_S, "launch")
        recorded_pids.add(helper_pid)
        _save(
            evidence,
            "launchctl-print.txt",
            _run(["launchctl", "print", f"{_launchctl_domain()}/{label}"], check=False).stdout,
        )
        phase_pass("launch", f"helper pid {helper_pid}, ping={ping}")

        # ---- chain ---------------------------------------------------------
        _wait_for("root socket", (run_dir / _ROOT_SOCKET).exists, _ROOT_WAIT_S, "chain")
        status = _wait_for(
            "root running", lambda: _status_if_running(root_status), _ROOT_WAIT_S, "chain"
        )
        root_pid = int(status["root"]["pid"])
        recorded_pids.add(root_pid)
        initial_units = {unit_id: int(_unit_entry(status, unit_id)["pid"]) for unit_id in _UNIT_IDS}
        recorded_pids.update(initial_units.values())

        helper_ppid = _ppid_of(helper_pid)
        root_ppid = _ppid_of(root_pid)
        if root_ppid != helper_pid:
            _fail("chain", f"root ppid {root_ppid} != helper pid {helper_pid}")
        if helper_ppid != 1:
            _fail("chain", f"helper ppid {helper_ppid} is not launchd(1)")
        for unit_id, unit_pid in initial_units.items():
            unit_ppid = _ppid_of(unit_pid)
            if unit_ppid != root_pid:
                _fail("chain", f"unit {unit_id} ppid {unit_ppid} != root pid {root_pid}")
        _save(
            evidence,
            "ps-chain.txt",
            _ps_line(helper_pid, root_pid, *initial_units.values())
            + f"\nhelper ppid={helper_ppid} root ppid={root_ppid}"
            + "".join(f" {unit_id} ppid={_ppid_of(pid)}" for unit_id, pid in initial_units.items()),
        )
        _save(evidence, "root-status-initial.json", json.dumps(status, indent=2))
        _save(evidence, "keeper-status-initial.json", json.dumps(helper_root_status(), indent=2))
        phase_pass(
            "chain",
            f"launchd -> helper {helper_pid} -> root {root_pid} -> units {list(initial_units.values())}",
        )

        # ---- attribution (F11) --------------------------------------------
        if args.skip_attribution:
            print("PHASE attribute: SKIP (--skip-attribution)")
        else:
            unit_results = {}
            for unit_id in initial_units:
                results_path = workdir / f"unit-results-{unit_id}.json"
                _wait_for(
                    f"unit {unit_id} preflight results", results_path.exists, 30.0, "attribute"
                )
                unit_results[unit_id] = json.loads(results_path.read_text())
            time.sleep(2.0)
            for unit_id, results in unit_results.items():
                if int(results["ppid"]) != root_pid:
                    _fail(
                        "attribute", f"unit {unit_id} ppid {results['ppid']} != root pid {root_pid}"
                    )
                _save(evidence, f"unit-results-{unit_id}.json", json.dumps(results, indent=2))
                _save(
                    evidence, f"unit-log-{unit_id}.txt", _tail(run_dir / "logs" / f"{unit_id}.log")
                )
            log_text = _run(
                [
                    "/usr/bin/log",
                    "show",
                    "--last",
                    "5m",
                    "--style",
                    "compact",
                    "--predicate",
                    'eventMessage CONTAINS "AUTHREQ_ATTRIBUTION"',
                ],
                timeout=120.0,
            ).stdout
            _save(evidence, "tccd-attribution-window.txt", log_text)
            attributed = []
            for unit_id, unit_pid in initial_units.items():
                matched = None
                for line in log_text.splitlines():
                    found = _ATTRIBUTION_RE.search(line)
                    if found and int(found.group("requesting_pid")) == unit_pid:
                        matched = found
                        break
                if matched is None:
                    _fail(
                        "attribute",
                        f"no AUTHREQ_ATTRIBUTION line for unit {unit_id} pid {unit_pid}; "
                        "see evidence/tccd-attribution-window.txt",
                    )
                if int(matched.group("responsible_pid")) != helper_pid:
                    _fail(
                        "attribute",
                        f"unit {unit_id} attributed to pid {matched.group('responsible_pid')} "
                        f"({matched.group('responsible_id')}), not helper {helper_pid}",
                    )
                attributed.append(f"{unit_id}={matched.group('responsible_id')}")
            prompting = [
                line
                for line in log_text.splitlines()
                if "AUTHREQ_PROMPTING" in line and re.search(rf"pid={helper_pid}\b", line)
            ]
            if prompting:
                _fail("attribute", f"unexpected permission prompt line: {prompting[0][:200]}")
            phase_pass(
                "attribute", f"units resolve to helper {helper_pid} ({', '.join(attributed)})"
            )

        # ---- conflict (helper crash) --------------------------------------
        # F12b (task #3380): sample the whole tree + TCC attribution across the
        # helper death/replacement window at five points -- h0 before the kill
        # (control: every request must resolve to the old helper), h1 right
        # after it, h2 once the relaunched helper reports `conflict`, h3 after
        # the orphan tree is closed, h4 once the re-seed is stable (re-seeded
        # units must resolve to the new helper). h1-h3 are the measurement:
        # what responsibility looks like between death and replacement.
        conflict_f12: dict[str, Any] = {"points": {}}
        ab_units = list(initial_units.items())
        ab_chain = [helper_pid, root_pid, *initial_units.values()]
        if args.sample_conflict:
            conflict_f12["points"]["h0"] = _f12_point(workdir, "h0", ab_units, ab_chain)
        _kill(helper_pid, signal.SIGKILL)
        if args.sample_conflict:
            conflict_f12["kill_ts"] = round(time.time(), 3)
            conflict_f12["points"]["h1"] = _f12_point(workdir, "h1", ab_units, ab_chain)
        new_helper_pid = _wait_for(
            "helper relaunched",
            lambda: _relaunched_helper(label, helper_pid),
            _HELPER_WAIT_S,
            "conflict",
        )
        recorded_pids.add(new_helper_pid)
        conflict = _wait_for(
            "keeper conflict",
            lambda: _status_if_conflict(helper_root_status),
            _ROOT_WAIT_S,
            "conflict",
        )
        if int(conflict["conflict"]["pid"]) != root_pid:
            _fail("conflict", f"conflict pid {conflict['conflict']['pid']} != orphan {root_pid}")
        _save(evidence, "keeper-status-conflict.json", json.dumps(conflict, indent=2))
        if args.sample_conflict:
            conflict_f12["points"]["h2"] = _f12_point(
                workdir, "h2", ab_units, [new_helper_pid, *ab_chain]
            )

        # The helper never signals a root it did not seed: a plain stop and a
        # forced one (raw wire -- the client has no force) are both refused.
        refusals = (
            _refusal(lambda: helper_client.stop_root(sock_path=helper_sock)),
            _refusal(lambda: helper_client._call("root_stop", force=True, sock_path=helper_sock)),
        )
        if "native recovery is required" not in refusals[0] or "force" not in refusals[1]:
            _fail("conflict", f"root_stop over the orphan root was not refused: {refusals}")
        _save(evidence, "conflict-refusals.txt", "\n".join(refusals))

        # Native recovery: the orphan closes its own tree through its control
        # socket; once the run dir is free the keeper seeds a fresh root itself.
        if _root_call(run_dir, "shutdown") != {"shutdown_requested": True}:
            _fail("conflict", "the orphan root did not accept its own shutdown")
        orphan_tree = [root_pid, *initial_units.values()]
        _wait_for(
            "orphan tree closed",
            lambda: not any(_pid_alive(pid) for pid in orphan_tree),
            _RESTART_WAIT_S,
            "conflict",
        )
        if args.sample_conflict:
            conflict_f12["points"]["h3"] = _f12_point(
                workdir, "h3", ab_units, [new_helper_pid, *ab_chain]
            )
        reseeded = _wait_for(
            "reseeded root",
            lambda: _status_if_keeper_running(helper_root_status),
            _ROOT_WAIT_S,
            "conflict",
        )
        reseeded_pid = int(reseeded["pid"])
        recorded_pids.add(reseeded_pid)
        reseeded_status = _wait_for(
            "reseeded tree",
            lambda: _status_if_running(root_status, excluding=root_pid),
            _ROOT_WAIT_S,
            "conflict",
        )
        reseeded_units = {
            unit_id: int(_unit_entry(reseeded_status, unit_id)["pid"]) for unit_id in _UNIT_IDS
        }
        recorded_pids.update(reseeded_units.values())
        if _ppid_of(reseeded_pid) != new_helper_pid:
            _fail("conflict", "reseeded root is not the relaunched helper's child")
        _save(evidence, "keeper-status-reseeded.json", json.dumps(reseeded, indent=2))
        _save(
            evidence,
            "conflict-reseed.txt",
            _ps_line(new_helper_pid, reseeded_pid, *reseeded_units.values())
            + f"\nreseeded root {reseeded_pid} is child of helper {new_helper_pid}",
        )
        if args.sample_conflict:
            time.sleep(_F12_STABLE_S)
            conflict_f12["points"]["h4"] = _f12_point(
                workdir,
                "h4",
                [*ab_units, *reseeded_units.items()],
                [new_helper_pid, reseeded_pid, *reseeded_units.values()],
            )
            conflict_f12.update(
                {
                    "old_helper_pid": helper_pid,
                    "new_helper_pid": new_helper_pid,
                    "orphan_root_pid": root_pid,
                    "reseeded": reseeded_units,
                }
            )
            _f12_finish(
                evidence,
                conflict_f12,
                "f12-conflict",
                [
                    ("h0", list(initial_units.values()), helper_pid),
                    ("h4", list(reseeded_units.values()), new_helper_pid),
                ],
            )
        phase_pass(
            "conflict", f"orphan {root_pid} refused + closed natively; reseeded root {reseeded_pid}"
        )

        # ---- restart (root crash) -----------------------------------------
        f12: dict[str, Any] = {"points": {}}
        survivors = list(reseeded_units.items())
        chain = [new_helper_pid, reseeded_pid, *reseeded_units.values()]
        restarts_before = int(helper_root_status()["restarts"])
        if args.sample_restart:
            f12["points"]["pre"] = _f12_point(workdir, "pre", survivors, chain)
        _kill(reseeded_pid, signal.SIGKILL)
        if args.sample_restart:
            f12["kill_ts"] = round(time.time(), 3)
            f12["points"]["gap"] = _f12_point(workdir, "gap", survivors, chain)
        keeper = _wait_for(
            "replacement root refused",
            lambda: _status_if_refused(helper_root_status, restarts_before),
            _RESTART_WAIT_S,
            "restart",
        )
        if "custody requires reconciliation" not in (workdir / "root.stderr.log").read_text():
            _fail("restart", "the replacement root did not refuse on retained service custody")
        dead_units = {unit_id: pid for unit_id, pid in survivors if not _pid_alive(pid)}
        if dead_units:
            _fail("restart", f"units died with the root: {dead_units}")
        probes = _probe_pids(probe)
        if probes != set(reseeded_units.values()):
            _fail("restart", f"unit probes {sorted(probes)} are not just the survivors")
        if args.sample_restart:
            f12["points"]["refused"] = _f12_point(workdir, "refused", survivors, chain)
            time.sleep(_F12_STABLE_S)
            f12["points"]["stable"] = _f12_point(workdir, "stable", survivors, chain)
            f12.update({"helper_pid": new_helper_pid, "root_pid": reseeded_pid, "keeper": keeper})
            _f12_finish(
                evidence,
                f12,
                "f12-restart",
                [
                    (point, list(reseeded_units.values()), new_helper_pid)
                    for point in ("pre", "gap", "refused", "stable")
                ],
            )
        _save(
            evidence,
            "restart.txt",
            _ps_line(new_helper_pid, *reseeded_units.values())
            + f"\nroot {reseeded_pid} killed; unit probes alive: {sorted(probes)}",
        )
        _save(evidence, "keeper-status-restart.json", json.dumps(keeper, indent=2))
        phase_pass(
            "restart",
            f"root {reseeded_pid} killed; units kept running; replacement refused on custody"
            + ("; F12 sampling saved" if args.sample_restart else ""),
        )

        print("\nSMOKE PASS: " + ", ".join(phases))
        return 0
    except SmokeError as failure:
        print(str(failure))
        if helper_sock.exists():
            with contextlib.suppress(Exception):
                helper_client.stop_root(sock_path=helper_sock)
        print(f"helper stderr tail:\n{_tail(workdir / 'helper.stderr.log')}")
        print(f"root stderr tail:\n{_tail(workdir / 'root.stderr.log')}")
        print(f"evidence retained at: {evidence}")
        return 1
    except Exception as unexpected:  # top-level smoke guard
        print(f"FAIL(phase=unexpected): {unexpected!r}")
        print(f"helper stderr tail:\n{_tail(workdir / 'helper.stderr.log')}")
        print(f"root stderr tail:\n{_tail(workdir / 'root.stderr.log')}")
        print(f"evidence retained at: {evidence}")
        return 1
    finally:
        _bootout(label)
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            alive = [pid for pid in recorded_pids if _pid_alive(pid)]
            if not alive:
                break
            for pid in alive:
                _kill(pid, signal.SIGTERM)
            time.sleep(0.25)
        for pid in [pid for pid in recorded_pids if _pid_alive(pid)]:
            _kill(pid, signal.SIGKILL)
        if args.cleanup:
            shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
