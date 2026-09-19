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
  .venv/bin/python scripts/tcc-onboard-helper-grants.py --tier L2  # target tier (design v1)
  .venv/bin/python scripts/tcc-onboard-helper-grants.py --items folders
  .venv/bin/python scripts/tcc-onboard-helper-grants.py --timeout 180
  .venv/bin/python scripts/tcc-onboard-helper-grants.py --fill-pending --confirm-user-present

Tiers: --tier names the machine's target authorization set (design v1). L0
refuses to probe or trigger at all (maintenance windows); L1..L3 grow the
item set. Extended groups (appdata, media, icloud, fda, devtools) always
have their grant state read from the same preflight matrix.

--fill-pending adds a best-effort trigger for an appdata / media / icloud
group that still lacks its grant: a helper-spawned child scans the guarded
surface directly, and the run waits for the decision and rechecks the
preflight matrix. The methods are EXPERIMENTAL -- appdata replays the
surface evidenced on macmini 2026-09-14, media and icloud are first-use
candidates; the archive in docs/conventions/tcc-helper-onboarding.md is the
authority on evidence levels. Run only with the user at the machine
(--confirm-user-present is required: a pending dialog blocks synthesized
input machine-wide). fda and devtools are never attempted, by decision.

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

IMPLEMENTED_GROUPS = ("folders", "apple-events", "sr-ax")
"""Groups this build can inventory and trigger end to end."""

EXTENDED_GROUPS: dict[str, tuple[str, ...]] = {
    # Extended-tier groups: their grant state is always preflight-readable.
    # appdata/media/icloud gain a best-effort trigger under --fill-pending;
    # fda/devtools are never attempted (see NEVER_TRIGGERED_GROUPS).
    "appdata": ("kTCCServiceSystemPolicyAppData",),
    "media": ("kTCCServiceMediaLibrary", "kTCCServicePhotos"),
    "icloud": ("kTCCServiceFileProviderDomain", "kTCCServiceUbiquity"),
    "fda": ("kTCCServiceSystemPolicyAllFiles",),
    "devtools": ("kTCCServiceDeveloperTool",),
}

FILL_SPECS: dict[str, tuple[tuple[str, str, str], ...]] = {
    # --fill-pending attempts per fillable extended group: (target id, TCC
    # service, guarded surface under $HOME). A helper-spawned child scans the
    # surface directly (the folder rows' spawn-child pattern, extended to a
    # bounded scan) and the attempt's outcome is rechecked against its
    # service in the preflight matrix. Evidence levels: the trigger-method
    # archive in docs/conventions/tcc-helper-onboarding.md.
    "appdata": (("appdata", "kTCCServiceSystemPolicyAppData", "Library/Application Support"),),
    "media": (
        ("media-music", "kTCCServiceMediaLibrary", "Music"),
        ("media-photos", "kTCCServicePhotos", "Pictures/Photos Library.photoslibrary"),
    ),
    "icloud": (
        (
            "icloud-clouddocs",
            "kTCCServiceFileProviderDomain",
            "Library/Mobile Documents/com~apple~CloudDocs",
        ),
    ),
}

NEVER_TRIGGERED_GROUPS: tuple[str, ...] = ("fda", "devtools")
"""Extended groups with no trigger method by decision; read-only, always."""

ITEM_GROUPS = IMPLEMENTED_GROUPS + tuple(EXTENDED_GROUPS)
"""Every group name accepted by --items / --tier."""

TIER_GROUPS: dict[str, tuple[str, ...]] = {
    "L0": (),  # silent posture: no probing, no triggering (maintenance windows)
    "L1": IMPLEMENTED_GROUPS,
    "L2": (*IMPLEMENTED_GROUPS, "appdata", "media", "icloud"),
    "L3": (*IMPLEMENTED_GROUPS, "appdata", "media", "icloud", "fda", "devtools"),
}
"""The tier model (design v1): a machine's target authorization set."""

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

PREFLIGHT_SERVICES = (
    [item[1] for item in FOLDER_ITEMS]
    + ["kTCCServiceScreenCapture"]
    + [service for services in EXTENDED_GROUPS.values() for service in services]
)

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

_SCAN_CHILD = '''"""Helper-spawned child: bounded scan of a guarded tree (the fill trigger)."""
import os
import sys

MAX_DIRS = 64
MAX_READS = 32
READ_BYTES = 16

target, result_path = sys.argv[1], sys.argv[2]
visited = 0
sampled = 0


def scan(path, depth):
    global visited, sampled
    if visited >= MAX_DIRS:
        return
    visited += 1
    for name in sorted(os.listdir(path))[:MAX_DIRS]:
        if visited >= MAX_DIRS:
            return
        full = os.path.join(path, name)
        if os.path.isdir(full):
            if depth > 1:
                scan(full, depth - 1)
        elif sampled < MAX_READS:
            with open(full, "rb") as handle:
                handle.read(READ_BYTES)
            sampled += 1


try:
    scan(target, 2)
    text = "granted dirs=%d files=%d" % (visited, sampled)
except FileNotFoundError:
    text = "missing %s" % target
except PermissionError as exc:
    text = "denied %s" % exc
except OSError as exc:
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


def groups_for_tier(tier: str | None) -> tuple[str, ...]:
    """Item groups implied by ``--tier``; the legacy implemented set when None."""
    if tier is None:
        return IMPLEMENTED_GROUPS
    return TIER_GROUPS[tier]


def fill_request_error(
    *, fill_pending: bool, confirm_user_present: bool, check_only: bool
) -> str | None:
    """Refusal message for an incoherent --fill-pending request; None when it may run."""
    if not fill_pending:
        return None
    if check_only:
        return (
            "--check is inventory-only and never triggers a dialog;"
            " --fill-pending asks to trigger -- pass one or the other"
        )
    if not confirm_user_present:
        return (
            "--fill-pending triggers system dialogs; pass --confirm-user-present"
            " (user at the machine) to attest they can answer them"
        )
    return None


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


def classify_touch(text: str) -> str:
    """Outcome of a direct-touch child (folder rows and fill scans alike)."""
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


RESOLVED_STATUSES = frozenset({"granted", "already granted"})
"""`statuses` spellings that count as resolved; every other value is unresolved."""


def count_unresolved(statuses: dict[str, str]) -> int:
    """How many reported items are unresolved -- the report's one count rule."""
    return sum(1 for status in statuses.values() if status not in RESOLVED_STATUSES)


def state_status(services: tuple[str, ...], matrix: dict[str, str], note: str) -> str:
    """Item status for an extended group: granted only when every service is."""
    states = {service: matrix.get(service, "unknown") for service in services}
    if all(value == "granted" for value in states.values()):
        return "granted"
    detail = ", ".join(f"{service}={value}" for service, value in sorted(states.items()))
    return f"unresolved ({detail}; {note})"


def extended_note(group: str, attempted: set[str]) -> str:
    """How an extended group's trigger stands -- the unresolved-state suffix."""
    if group in NEVER_TRIGGERED_GROUPS:
        return "no trigger method by decision"
    if group in attempted:
        return "fill attempted (experimental method)"
    return "trigger method available via --fill-pending (experimental)"


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
        help="inventory only: preflight folder + system + extended services, never trigger a dialog",
    )
    parser.add_argument(
        "--fill-pending",
        action="store_true",
        help=(
            "experimental: best-effort triggers for appdata/media/icloud groups"
            " still missing their grant (requires --confirm-user-present;"
            " methods + evidence levels: docs/conventions/tcc-helper-onboarding.md)"
        ),
    )
    parser.add_argument(
        "--confirm-user-present",
        action="store_true",
        help=(
            "attest the user is at the machine; required with --fill-pending (a"
            " pending dialog blocks synthesized input machine-wide until answered)"
        ),
    )
    target = parser.add_mutually_exclusive_group()
    target.add_argument(
        "--tier",
        choices=tuple(TIER_GROUPS),
        help=(
            "target tier (design v1): sets the item groups for the run;"
            " L0 refuses to probe or trigger, L1..L3 grow the set"
        ),
    )
    target.add_argument(
        "--items",
        help="comma-separated subset of: "
        + ",".join(IMPLEMENTED_GROUPS)
        + " (extended groups, state always read from the preflight matrix: "
        + ",".join(EXTENDED_GROUPS)
        + "; appdata/media/icloud can additionally be attempted with --fill-pending)",
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

    if args.tier == "L0":
        # Silent posture: nothing is probed and nothing is triggered.
        print("tier L0 (silent): no inventory probe and no triggers are performed.")
        return 0

    fill_error = fill_request_error(
        fill_pending=args.fill_pending,
        confirm_user_present=args.confirm_user_present,
        check_only=args.check,
    )
    if fill_error is not None:
        print(f"FAIL: {fill_error}")
        return 2

    raw_items = args.items if args.items is not None else ",".join(groups_for_tier(args.tier))
    items = [part.strip() for part in raw_items.split(",") if part.strip()]
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

    extended = [group for group in items if group in EXTENDED_GROUPS]
    if args.tier is not None:
        print(f"tier {args.tier} -- groups: {', '.join(items)}")
    if extended:
        print(
            "extended groups (state read via preflight; trigger methods:"
            " docs/conventions/tcc-helper-onboarding.md):"
        )
        for group in extended:
            states = ", ".join(
                f"{service}={matrix.get(service, 'unknown')}" for service in EXTENDED_GROUPS[group]
            )
            print(f"  {group}: {states}")

    statuses: dict[str, str] = {}

    if "folders" in items:
        print("\n== file & folders block ==")
        for item_id, service, folder in FOLDER_ITEMS:
            if matrix.get(service) == "granted":
                statuses[item_id] = "already granted"
                print(f"  [{item_id}] already granted, skipping")
                continue
            if args.check:
                statuses[item_id] = matrix.get(service, "unknown")
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
            statuses[item_id] = classify_touch(text)
            print(f"  [{item_id}] -> {statuses[item_id]}")

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
            print(
                "  [screen-recording] MISSING -- grant AvaPermissionsHelper in System Settings >"
                " Privacy & Security > Screen Recording, then restart the helper."
            )
        if ping.get("ax_trusted"):
            statuses["accessibility"] = "granted"
            print("  [accessibility] granted")
        else:
            statuses["accessibility"] = "missing"
            print(
                "  [accessibility] MISSING -- grant AvaPermissionsHelper in System Settings >"
                " Privacy & Security > Accessibility."
            )

    fill_results: dict[str, str] = {}
    fill_attempted: set[str] = set()
    if args.fill_pending:
        if extended:
            print("\n== fill-pending (experimental methods; user at the machine) ==")
            for group in extended:
                if group in NEVER_TRIGGERED_GROUPS:
                    print(f"  [{group}] never attempted (no trigger method by decision)")
                    continue
                for target_id, service, rel in FILL_SPECS[group]:
                    if matrix.get(service) == "granted":
                        print(f"  [{target_id}] already granted, skipping")
                        continue
                    fill_attempted.add(group)
                    child = workdir / f"fill-{target_id}.child.py"
                    child.write_text(_SCAN_CHILD)
                    result_path = workdir / f"fill-{target_id}-{run_id}.result"
                    text = wait_for_item(
                        client,
                        workdir,
                        run_id,
                        f"fill-{target_id}",
                        [sys.executable, str(child), str(Path.home() / rel), str(result_path)],
                        result_path,
                        args.timeout,
                        service,
                    )
                    fill_results[target_id] = text
                    print(f"  [{target_id}] -> {classify_touch(text)}")
            matrix = preflight_matrix(client, workdir, f"{run_id}-after-fill")
            print("grant state after fill attempts (preflight re-read):")
            for service in PREFLIGHT_SERVICES:
                print(f"  {service}: {matrix.get(service, 'unknown')}")
        else:
            print("\n== fill-pending: no extended groups in this run (nothing to fill) ==")

    for group in extended:
        statuses[group] = state_status(
            EXTENDED_GROUPS[group], matrix, extended_note(group, fill_attempted)
        )

    unresolved_count = count_unresolved(statuses)
    unresolved = unresolved_count > 0

    report = {
        "run_id": run_id,
        "host": os.uname().nodename,
        "tier": args.tier,
        "groups": items,
        "extended_groups": extended,
        "check_only": args.check,
        "fill_pending": args.fill_pending,
        "confirm_user_present": args.confirm_user_present,
        "matrix": matrix,
        "statuses": statuses,
        "fill_attempted": sorted(fill_attempted),
        "fill_results": fill_results,
        "unresolved": unresolved,
        "unresolved_count": unresolved_count,
    }
    (workdir / f"report-{run_id}.json").write_text(json.dumps(report, indent=2) + "\n")

    print("\n== summary ==")
    for item_id, value in statuses.items():
        print(f"  {item_id}: {value}")
    if args.check:
        print("note: --check never shows AppleEvents rows (see the docstring).")
    if extended:
        print(
            "extended groups (state read; fill: --fill-pending; methods archive:"
            " docs/conventions/tcc-helper-onboarding.md): " + ", ".join(extended)
        )
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
