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
  restart    kill -9 the root: the helper restarts it; the old unit keeps
             running (the design's "lose attribution, not service")
  conflict   kill -9 the helper: launchd relaunches it; the relaunched helper
             finds the orphan root, rests in `conflict` (no double-spawn, no
             kill), `root_stop` without force is refused, force disposes the
             orphan, and an explicit re-seed brings a fresh tree up

Nothing here touches production: the binary is throwaway-signed, every path
lives under the workdir, and the launchd job uses its own test label (never
the production helper's). The helper's first-run registration nudge is
disabled via AVA_PERMISSIONS_HELPER_SKIP_REGISTRATION=1 -- an Aqua-session
helper with a fresh code identity would otherwise raise TCC dialogs on a
machine nobody is sitting at.

Exit 0 = every phase passed. Evidence (ps/logs/status snapshots) is retained
under the workdir; pass --cleanup to remove it. Run with the repository venv,
on macOS, from a checkout that contains services/ava_root (dev/CI only).
"""

from __future__ import annotations

import argparse
import contextlib
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

_ATTRIBUTION_RE = re.compile(
    r"responsible=\{TCCDProcess: identifier=(?P<responsible_id>[^,]*), pid=(?P<responsible_pid>\d+)"
    r".*?(?P<role>requesting|accessing)=\{TCCDProcess: identifier=(?P<requesting_id>[^,]*), "
    r"pid=(?P<requesting_pid>\d+)"
)

_UNIT_PROBE = '''\
\
"""Unit probe: side-effect-free preflight queries + heartbeat.

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
    results = {}
    for service in SERVICES:
        results[service] = preflight(service)
    with open(results_path, "w") as handle:
        json.dump({"pid": os.getpid(), "ppid": os.getppid(), "services": results}, handle)
    while True:
        with open(beat_path, "a") as handle:
            handle.write("%.0f\\n" % time.time())
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


def _seed_config(workdir: Path) -> dict:
    return json.loads((workdir / "seed.json").read_text())


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


def _status_if_state(helper_root_status, wanted: str):
    try:
        status = helper_root_status()
    except Exception:
        return None
    return status if status.get("state") == wanted else None


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


def _root_status(run_dir: Path) -> dict[str, Any]:
    """Read the K1 tree snapshot over the raw control socket (stdlib only).

    The smoke drives the root as a black box — process tree, socket, status
    verb — so it never depends on the root package's client API.
    """
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(10.0)
        sock.connect(str(run_dir / _ROOT_SOCKET))
        sock.sendall(b'{"verb": "status"}\n')
        line = sock.makefile("rb").readline()
    response = json.loads(line)
    if not response.get("ok"):
        _fail("root", f"status refused: {response}")
    return cast("dict[str, Any]", response["result"])


def main() -> int:  # noqa: PLR0915 - one bounded smoke lifecycle: every phase, wait, and teardown live together on purpose
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument(
        "--workdir",
        default="/tmp/two-section-chain-smoke",  # noqa: S108 - scratch evidence dir, never secret
    )
    parser.add_argument("--label", default=DEFAULT_LABEL)
    parser.add_argument("--python", default=sys.executable, help="interpreter for root + units")
    parser.add_argument("--skip-attribution", action="store_true", help="skip the F11 tccd check")
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
        return _root_status(run_dir)

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
        # satisfy this run's waits (the attribution split and the graceful-stop
        # check read files, not memory).
        for stale in (
            "unit-results.json",
            "unit.beat",
            "root.stdout.log",
            "root.stderr.log",
            "helper.stdout.log",
            "helper.stderr.log",
        ):
            (workdir / stale).unlink(missing_ok=True)
        probe = workdir / "unit_probe.py"
        probe.write_text(_UNIT_PROBE)
        manifests = workdir / "manifests.json"
        manifests.write_text(
            json.dumps(
                {
                    "units": [
                        {
                            "id": "heartbeat",
                            "exec": [
                                args.python,
                                str(probe),
                                str(workdir / "unit-results.json"),
                                str(workdir / "unit.beat"),
                            ],
                            "restart": "always",
                            "attach": "root",
                        }
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
        (workdir / "seed.json").write_text(json.dumps(seed, indent=2))
        plist = {
            "Label": label,
            "ProgramArguments": [str(exe)],
            "EnvironmentVariables": {
                "AVA_PERMISSIONS_HELPER_SOCKET": str(helper_sock),
                "AVA_PERMISSIONS_HELPER_ROOT_SEED": str(workdir / "seed.json"),
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
        unit_pid = int(_unit_entry(status, "heartbeat")["pid"])
        recorded_pids.add(unit_pid)

        helper_ppid = _ppid_of(helper_pid)
        root_ppid = _ppid_of(root_pid)
        unit_ppid = _ppid_of(unit_pid)
        if root_ppid != helper_pid:
            _fail("chain", f"root ppid {root_ppid} != helper pid {helper_pid}")
        if unit_ppid != root_pid:
            _fail("chain", f"unit ppid {unit_ppid} != root pid {root_pid}")
        if helper_ppid != 1:
            _fail("chain", f"helper ppid {helper_ppid} is not launchd(1)")
        _save(
            evidence,
            "ps-chain.txt",
            _ps_line(helper_pid, root_pid, unit_pid)
            + f"\nhelper ppid={helper_ppid} root ppid={root_ppid} unit ppid={unit_ppid}",
        )
        _save(evidence, "root-status-initial.json", json.dumps(status, indent=2))
        _save(evidence, "keeper-status-initial.json", json.dumps(helper_root_status(), indent=2))
        phase_pass("chain", f"launchd -> helper {helper_pid} -> root {root_pid} -> unit {unit_pid}")

        # ---- attribution (F11) --------------------------------------------
        if args.skip_attribution:
            print("PHASE attribute: SKIP (--skip-attribution)")
        else:
            results_path = workdir / "unit-results.json"
            _wait_for("unit preflight results", results_path.exists, 30.0, "attribute")
            time.sleep(2.0)
            unit_results = json.loads(results_path.read_text())
            if int(unit_results["ppid"]) != root_pid:
                _fail("attribute", f"unit ppid {unit_results['ppid']} != root pid {root_pid}")
            _save(evidence, "unit-results.json", json.dumps(unit_results, indent=2))
            _save(evidence, "unit-log.txt", _tail(run_dir / "logs" / "heartbeat.log"))
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
            matched = None
            for line in log_text.splitlines():
                found = _ATTRIBUTION_RE.search(line)
                if found and int(found.group("requesting_pid")) == unit_pid:
                    matched = found
                    break
            if matched is None:
                _fail(
                    "attribute",
                    f"no AUTHREQ_ATTRIBUTION line for unit pid {unit_pid}; "
                    "see evidence/tccd-attribution-window.txt",
                )
            if int(matched.group("responsible_pid")) != helper_pid:
                _fail(
                    "attribute",
                    f"unit attributed to pid {matched.group('responsible_pid')} "
                    f"({matched.group('responsible_id')}), not helper {helper_pid}",
                )
            prompting = [
                line
                for line in log_text.splitlines()
                if "AUTHREQ_PROMPTING" in line and re.search(rf"pid={helper_pid}\b", line)
            ]
            if prompting:
                _fail("attribute", f"unexpected permission prompt line: {prompting[0][:200]}")
            phase_pass(
                "attribute",
                f"unit {unit_pid} resolves to helper {helper_pid} "
                f"(identifier={matched.group('responsible_id')})",
            )

        # ---- restart (root crash) -----------------------------------------
        _kill(root_pid, signal.SIGKILL)
        restarted = _wait_for(
            "root restarted",
            lambda: _status_if_running(root_status, excluding=root_pid),
            _RESTART_WAIT_S,
            "restart",
        )
        new_root_pid = int(restarted["root"]["pid"])
        recorded_pids.add(new_root_pid)
        if not _pid_alive(unit_pid):
            _fail("restart", f"old unit pid {unit_pid} died with the root")
        keeper = helper_root_status()
        if int(keeper["restarts"]) < 1:
            _fail("restart", f"keeper restarts={keeper['restarts']} did not record the crash")
        new_unit_pid = int(_unit_entry(restarted, "heartbeat")["pid"])
        recorded_pids.add(new_unit_pid)
        _save(
            evidence,
            "restart.txt",
            _ps_line(helper_pid, new_root_pid, unit_pid, new_unit_pid)
            + f"\nold unit {unit_pid} alive after root crash: {_pid_alive(unit_pid)}"
            + f"\nnew root {new_root_pid}, new unit {new_unit_pid}",
        )
        _save(evidence, "keeper-status-restart.json", json.dumps(keeper, indent=2))
        phase_pass(
            "restart", f"root {root_pid} -> {new_root_pid}; old unit {unit_pid} kept running"
        )

        # ---- conflict (helper crash) --------------------------------------
        _kill(helper_pid, signal.SIGKILL)
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
        if int(conflict["conflict"]["pid"]) != new_root_pid:
            _fail(
                "conflict",
                f"conflict pid {conflict['conflict']['pid']} != orphan root {new_root_pid}",
            )
        _save(evidence, "keeper-status-conflict.json", json.dumps(conflict, indent=2))

        refused = False
        try:
            helper_client.stop_root(sock_path=helper_sock)
        except Exception as exc:  # the refusal message is the assertion
            refused = "refusing to stop root" in str(exc)
        if not refused:
            _fail("conflict", "root_stop without force was not refused")

        helper_client.stop_root(force=True, sock_path=helper_sock)
        stopped = _wait_for(
            "orphan disposed",
            lambda: _status_if_state(helper_root_status, "stopped"),
            _RESTART_WAIT_S,
            "conflict",
        )
        _wait_for(
            "orphan root gone", lambda: not _pid_alive(new_root_pid), _RESTART_WAIT_S, "conflict"
        )
        _save(evidence, "keeper-status-disposed.json", json.dumps(stopped, indent=2))
        if (
            "stop signal received; stopping the tree"
            not in (workdir / "root.stderr.log").read_text()
        ):
            _fail(
                "conflict",
                "orphan root did not log a graceful SIGTERM stop (signal masked?)",
            )

        helper_client.seed_root(cast("Any", _seed_config(workdir)), sock_path=helper_sock)
        reseeded = _wait_for(
            "reseeded root",
            lambda: _status_if_keeper_running(helper_root_status),
            _ROOT_WAIT_S,
            "conflict",
        )
        reseeded_pid = int(reseeded["pid"])
        recorded_pids.add(reseeded_pid)
        reseeded_status = _wait_for(
            "reseeded tree", lambda: _status_if_running(root_status), _ROOT_WAIT_S, "conflict"
        )
        reseeded_unit_pid = int(_unit_entry(reseeded_status, "heartbeat")["pid"])
        recorded_pids.add(reseeded_unit_pid)
        if _ppid_of(reseeded_pid) != new_helper_pid:
            _fail("conflict", "reseeded root is not the relaunched helper's child")
        _save(
            evidence,
            "conflict-reseed.txt",
            _ps_line(new_helper_pid, new_root_pid, reseeded_pid, reseeded_unit_pid)
            + f"\nreseeded root {reseeded_pid} is child of helper {new_helper_pid}; unit {reseeded_unit_pid}",
        )
        if _pid_alive(unit_pid):
            _kill(unit_pid, signal.SIGTERM)
        phase_pass(
            "conflict", f"orphan {new_root_pid} detected + disposed; reseeded root {reseeded_pid}"
        )

        print("\nSMOKE PASS: " + ", ".join(phases))
        return 0
    except SmokeError as failure:
        print(str(failure))
        if helper_sock.exists():
            with contextlib.suppress(Exception):
                helper_client.stop_root(force=True, sock_path=helper_sock)
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
