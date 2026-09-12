#!/usr/bin/env python
"""One-pass TCC onboarding for the macOS permissions helper.

Why this exists: the helper grants (Desktop/Documents/Downloads, AppleEvents
targets, Screen Recording / Accessibility) must be in place BEFORE a host
enables the helper-spawn backend (AVA_PERMISSIONS_HELPER_SPAWN) -- flipping
first turns file access from the already-granted python identity into an
ungranted helper identity (dialogs or silent denials). Instead of meeting the
grants one at a time as workflows trip over them, this tool bumps them all in
one sitting with the user present: it inventories the current grants with
side-effect-free preflight queries, triggers each missing request, waits for
the decision, and reports a final matrix.

Mechanics:
- Every trigger is a child process spawned through the permissions helper
  (services.permissions_helper.client.spawn_process), so tccd attributes the
  request to com.ava.permissions-helper -- the identity the grant must land
  on. The inventory probe reuses the helper-spawned preflight pattern of
  scripts/tcc-verify-spawn-chain.sh (zero dialogs, repeatable).
- Documents folder can NOT be triggered through the helper's file_list/read
  APIs: their path whitelist covers only Desktop, Downloads and .ava/incoming,
  and asking for ~/Documents fails with "outside whitelist" before tccd is
  reached. All folder triggers therefore use a spawned child that directly
  accesses the directory (os.listdir), which works uniformly for every folder
  service.
- AppleEvents rows are keyed per target app; each target is triggered by a
  spawned osascript child sending a benign "get version" command. These rows
  are granted only by a live user decision (dialog), so run this tool with the
  user at the machine. Screen Recording / Accessibility can not be requested
  programmatically at all: they are verified from the helper ping, and when
  missing the tool prints the System Settings path to fix them by hand.
- The helper must be a build carrying the nursery spawn wire method. Older
  builds answer "unknown method: spawn"; the tool reports that as "helper
  build needs a rebuild" instead of failing obscurely.

Usage:
  .venv/bin/python scripts/tcc-onboard-helper-grants.py            # interactive, all items
  .venv/bin/python scripts/tcc-onboard-helper-grants.py --check    # inventory only (no dialogs)
  .venv/bin/python scripts/tcc-onboard-helper-grants.py --items folders
  .venv/bin/python scripts/tcc-onboard-helper-grants.py --timeout 180

Exit codes: 0 = every requested item is granted/verified; 1 = unresolved
items remain (details in the report and at the workdir); 2 = setup failure
(not macOS, unknown --items, helper unreachable, missing repo venv, helper
build without the spawn wire method, probe timeout).
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

DEFAULT_WORKDIR = "/tmp/tcc-onboard-helper-grants"  # noqa: S108 - scratch evidence dir, never secret

ITEM_GROUPS = ("folders", "apple-events", "sr-ax")

FOLDER_ITEMS: tuple[tuple[str, str, str], ...] = (
    # (item id, TCC service, folder under $HOME)
    ("desktop", "kTCCServiceSystemPolicyDesktopFolder", "Desktop"),
    ("documents", "kTCCServiceSystemPolicyDocumentsFolder", "Documents"),
    ("downloads", "kTCCServiceSystemPolicyDownloadsFolder", "Downloads"),
)

APPLE_EVENT_TARGETS: tuple[tuple[str, str], ...] = (
    # (target app, benign AppleEvent script) -- "get version" touches no data
    ("Finder", 'tell application "Finder" to get version'),
    ("Terminal", 'tell application "Terminal" to get version'),
    ("System Events", 'tell application "System Events" to get version'),
    ("Safari", 'tell application "Safari" to get version'),
    ("Google Chrome", 'tell application "Google Chrome" to get version'),
)

PREFLIGHT_SERVICES = [item[1] for item in FOLDER_ITEMS] + [
    "kTCCServiceSystemPolicyAllFiles",
    "kTCCServiceScreenCapture",
]

POLL_S = 2.0
PREFLIGHT_EVERY_N_POLLS = 3
PROBE_WAIT_S = 10.0

_PREFLIGHT_PROBE = '''"""Helper-spawned probe: side-effect-free TCC preflight queries."""
import ctypes
import json
import sys

SERVICES = json.loads(sys.argv[1])
RESULT_WORDS = {0: "granted", 1: "denied", 2: "not-determined"}

_cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
_cf.CFStringCreateWithCString.restype = ctypes.c_void_p
_cf.CFStringCreateWithCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]
_tcc = ctypes.CDLL("/System/Library/PrivateFrameworks/TCC.framework/Versions/A/TCC")
_tcc.TCCAccessPreflight.restype = ctypes.c_int
_tcc.TCCAccessPreflight.argtypes = [ctypes.c_void_p, ctypes.c_void_p]


def preflight(service):
    cf_service = _cf.CFStringCreateWithCString(None, service.encode(), 0x08000100)
    return _tcc.TCCAccessPreflight(ctypes.c_void_p(cf_service), None)


results = {}
for service in SERVICES:
    code = preflight(service)
    results[service] = {"code": code, "result": RESULT_WORDS.get(code, "unknown")}
with open(sys.argv[2], "w") as handle:
    json.dump(results, handle, indent=2)
'''

_FOLDER_ACCESS_CHILD = '''"""Helper-spawned child: direct folder access (the TCC trigger)."""
import os
import sys

target, result_path = sys.argv[1], sys.argv[2]
try:
    names = os.listdir(target)
    text = "granted %d" % len(names)
except PermissionError:
    text = "denied"
except Exception as exc:  # noqa: BLE001 - report any failure verbatim to the driver
    text = "error %s: %s" % (type(exc).__name__, exc)
with open(result_path, "w") as handle:
    handle.write(text)
'''

_APPLE_EVENT_CHILD = '''"""Helper-spawned child: one benign AppleEvent to a target app."""
import subprocess
import sys

script, result_path = sys.argv[1], sys.argv[2]
try:
    done = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    if done.returncode == 0:
        text = "granted " + done.stdout.strip()[:120]
    else:
        text = "failed " + done.stderr.strip()[:200]
except Exception as exc:  # noqa: BLE001 - report any failure verbatim to the driver
    text = "error %s: %s" % (type(exc).__name__, exc)
with open(result_path, "w") as handle:
    handle.write(text)
'''


class OnboardError(RuntimeError):
    """A user-facing onboarding failure."""


def helper_client() -> Any:
    try:
        from services.permissions_helper import client
    except ImportError as exc:
        raise OnboardError(
            "run this script with the repository venv "
            "(e.g. .venv/bin/python scripts/tcc-onboard-helper-grants.py)"
        ) from exc
    return client


def spawn_child(client: Any, workdir: Path, name: str, argv: list[str], tag: str) -> int:
    from shared import session_env

    # The helper spawn contract wants the child's FULL environment; the
    # registry's session forward view is the sanctioned builder for it.
    # activate_venv=False: the child's cwd is the scratch workdir, outside
    # this checkout (see shared/session_env.py).
    child_env = session_env.forward_env_dict(activate_venv=False)
    try:
        result = client.spawn_process(
            name,
            argv,
            child_env,
            str(workdir),
            str(workdir / f"{tag}.stdout.log"),
            str(workdir / f"{tag}.stderr.log"),
        )
    except client.PermissionsHelperError as exc:
        message = str(exc)
        if "unknown method" in message:
            raise OnboardError(
                "helper build needs a rebuild: it does not support the spawn"
                f" wire method ({message!r}) -- rebuild or update the"
                " permissions helper, then re-run this tool."
            ) from exc
        raise OnboardError(f"helper spawn failed: {message}") from exc
    pid = result.get("pid")
    if not isinstance(pid, int):
        raise OnboardError(f"helper spawn returned no pid: {result!r}")
    return pid


def read_result(path: Path) -> str | None:
    try:
        return path.read_text().strip()
    except FileNotFoundError:
        return None


def preflight_matrix(client: Any, workdir: Path, run_id: str) -> dict[str, str]:
    probe = workdir / "tcc_preflight_probe.py"
    probe.write_text(_PREFLIGHT_PROBE)
    # Run-scoped result name: a late write from an earlier run's still-blocked
    # child can not land in this run's file.
    result_path = workdir / f"preflight-{run_id}.results.json"
    if result_path.exists():
        result_path.unlink()
    spawn_child(
        client,
        workdir,
        f"tcc-onboard-{run_id}-preflight",
        [sys.executable, str(probe), json.dumps(PREFLIGHT_SERVICES), str(result_path)],
        f"preflight-{run_id}",
    )
    deadline = time.monotonic() + PROBE_WAIT_S
    while time.monotonic() < deadline:
        text = read_result(result_path)
        if text is not None:
            data = json.loads(text)
            return {service: entry["result"] for service, entry in data.items()}
        time.sleep(0.25)
    raise OnboardError(
        f"preflight probe produced no result within {PROBE_WAIT_S:.0f}s"
        f" (see the child logs under {workdir})"
    )


def reap_child(client: Any, spawn_name: str) -> str:
    """Kill a child that is still waiting after a timeout (SIGTERM, then
    SIGKILL as needed) so no dialog is left pending past the run."""
    try:
        client.signal_session(name=spawn_name, sig=signal.SIGTERM)
        time.sleep(1.0)
        if client.session_has(spawn_name):
            client.signal_session(name=spawn_name, sig=signal.SIGKILL)
            time.sleep(0.5)
            if client.session_has(spawn_name):
                return "still alive after SIGKILL"
        return "reaped"
    except Exception as exc:
        return f"reap failed: {exc!r}"


def wait_for_item(
    client: Any,
    workdir: Path,
    run_id: str,
    tag: str,
    argv: list[str],
    result_path: Path,
    timeout_s: float,
    recheck_service: str | None,
) -> str:
    if result_path.exists():
        result_path.unlink()
    spawn_name = f"tcc-onboard-{run_id}-{tag}"
    spawn_child(client, workdir, spawn_name, argv, f"{tag}-{run_id}")
    print(f"  [{tag}] request sent -- click Allow in the system dialog (if shown).")
    deadline = time.monotonic() + timeout_s
    polls = 0
    while time.monotonic() < deadline:
        time.sleep(POLL_S)
        text = read_result(result_path)
        if text is not None:
            return text
        polls += 1
        if recheck_service is not None and polls % PREFLIGHT_EVERY_N_POLLS == 0:
            matrix = preflight_matrix(client, workdir, f"{run_id}-r{polls}")
            if matrix.get(recheck_service) == "granted":
                return "granted"
    print(f"  [{tag}] timed out after {timeout_s:.0f}s -- child {reap_child(client, spawn_name)}.")
    return "unresolved"


def classify_folder(text: str) -> str:
    if text.startswith("granted"):
        return "granted"
    if text.startswith("denied"):
        return "denied"
    return f"unresolved ({text})"


def classify_apple_event(text: str) -> str:
    if text.startswith("granted"):
        return "granted"
    if text.startswith("failed") and "-1743" in text:
        return "denied (-1743: user declined the Automation dialog)"
    return f"unresolved ({text})"


def main() -> int:  # noqa: PLR0915 - one bounded onboarding pass: every item and report live together
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument(
        "--workdir",
        default=DEFAULT_WORKDIR,
        help="scratch + evidence directory (retained unless --cleanup)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="inventory only: preflight the folder + system services, never trigger a dialog",
    )
    parser.add_argument(
        "--items",
        default=",".join(ITEM_GROUPS),
        help="comma-separated subset of: " + ",".join(ITEM_GROUPS),
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=90.0,
        help="seconds to wait for each dialog decision (default 90)",
    )
    parser.add_argument("--cleanup", action="store_true", help="remove the workdir at the end")
    args = parser.parse_args()

    if sys.platform != "darwin":
        print("FAIL: this onboarding tool is macOS-only (the permissions helper is).")
        return 2

    items = [part.strip() for part in args.items.split(",") if part.strip()]
    unknown = [part for part in items if part not in ITEM_GROUPS]
    if unknown:
        print(f"FAIL: unknown --items value(s): {', '.join(unknown)}")
        return 2

    workdir = Path(args.workdir).resolve()
    workdir.mkdir(parents=True, exist_ok=True)
    run_id = f"{time.strftime('%H%M%S')}-{os.getpid()}"

    client = helper_client()
    try:
        ping = client.ping()
    except Exception as exc:
        print(f"FAIL: permissions helper unreachable: {exc!r}")
        print("      install/start the helper first (see conventions/runbook.md).")
        return 2
    print(
        f"helper ping: ax_trusted={ping.get('ax_trusted')} "
        f"preflight_screen={ping.get('preflight_screen')} pong={ping.get('pong')}"
    )

    matrix = preflight_matrix(client, workdir, run_id)
    print("current helper grant state (preflight, zero dialogs):")
    for service in PREFLIGHT_SERVICES:
        print(f"  {service}: {matrix.get(service, 'unknown')}")

    statuses: dict[str, str] = {}
    unresolved = False

    if "folders" in items:
        print("\n== file & folders block ==")
        for item_id, service, folder in FOLDER_ITEMS:
            if matrix.get(service) == "granted":
                statuses[item_id] = "already granted"
                print(f"  [{item_id}] already granted, skipping")
                continue
            if args.check:
                statuses[item_id] = matrix.get(service, "unknown")
                unresolved = unresolved or statuses[item_id] != "granted"
                continue
            child = workdir / f"folder-{item_id}.child.py"
            child.write_text(_FOLDER_ACCESS_CHILD)
            result_path = workdir / f"folder-{item_id}-{run_id}.result"
            text = wait_for_item(
                client,
                workdir,
                run_id,
                item_id,
                [sys.executable, str(child), str(Path.home() / folder), str(result_path)],
                result_path,
                args.timeout,
                service,
            )
            statuses[item_id] = classify_folder(text)
            print(f"  [{item_id}] -> {statuses[item_id]}")
            unresolved = unresolved or statuses[item_id] != "granted"

    if "apple-events" in items and not args.check:
        print("\n== AppleEvents block (Automation dialogs, one per target app) ==")
        for target, script in APPLE_EVENT_TARGETS:
            item_id = f"apple-events:{target}"
            child = workdir / f"ae-{target.replace(' ', '_')}.child.py"
            child.write_text(_APPLE_EVENT_CHILD)
            result_path = workdir / f"ae-{target.replace(' ', '_')}-{run_id}.result"
            text = wait_for_item(
                client,
                workdir,
                run_id,
                f"ae-{target.replace(' ', '_')}",
                [sys.executable, str(child), script, str(result_path)],
                result_path,
                args.timeout,
                None,
            )
            statuses[item_id] = classify_apple_event(text)
            print(f"  [{item_id}] -> {statuses[item_id]}")
            unresolved = unresolved or statuses[item_id] != "granted"
    elif "apple-events" in items and args.check:
        print(
            "\n== AppleEvents block: skipped in --check (no silent way to read Automation rows) =="
        )

    if "sr-ax" in items:
        print("\n== Screen Recording / Accessibility ==")
        if ping.get("preflight_screen"):
            statuses["screen-recording"] = "granted"
            print("  [screen-recording] granted")
        else:
            statuses["screen-recording"] = "missing"
            unresolved = True
            print(
                "  [screen-recording] MISSING -- grant AvaPermissionsHelper in System Settings >"
                " Privacy & Security > Screen Recording, then restart the helper."
            )
        if ping.get("ax_trusted"):
            statuses["accessibility"] = "granted"
            print("  [accessibility] granted")
        else:
            statuses["accessibility"] = "missing"
            unresolved = True
            print(
                "  [accessibility] MISSING -- grant AvaPermissionsHelper in System Settings >"
                " Privacy & Security > Accessibility."
            )

    report = {
        "run_id": run_id,
        "host": os.uname().nodename,
        "check_only": args.check,
        "matrix": matrix,
        "statuses": statuses,
        "unresolved": unresolved,
    }
    (workdir / f"report-{run_id}.json").write_text(json.dumps(report, indent=2) + "\n")

    print("\n== summary ==")
    for item_id, value in statuses.items():
        print(f"  {item_id}: {value}")
    if args.check:
        print("note: --check never shows AppleEvents rows (see the docstring).")
    if unresolved:
        print(
            "ONBOARD INCOMPLETE -- resolve the items above, then re-run (it skips granted items)."
        )
    else:
        print("ONBOARD PASS -- all requested grants are in place.")
    print(f"evidence: {workdir}")
    if args.cleanup:
        import shutil

        shutil.rmtree(workdir, ignore_errors=True)
    return 1 if unresolved else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except OnboardError as exc:
        # Setup failures (missing repo venv, old helper build, probe timeout)
        # end as a clean message + exit 2 instead of a traceback.
        print(f"FAIL: {exc}")
        raise SystemExit(2) from exc
