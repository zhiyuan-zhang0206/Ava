#!/usr/bin/env bash
# Verify TCC attribution of a permissions-helper-spawned process.
#
# The probe is spawned through the permissions helper and issues three
# side-effect-free TCCAccessPreflight queries (Desktop folder, All Files,
# screen capture). tccd records every query as an AUTHREQ_ATTRIBUTION entry;
# this script reads those records back and confirms the spawned process
# resolves to com.ava.permissions-helper -- the attribution the helper-spawn
# path exists to provide.
#
# Why preflight queries: a real access (e.g. listing ~/Desktop) blocks on a
# permission prompt whenever the helper is not yet granted the service, and on
# macOS a TCC prompt also blocks synthesized input machine-wide. Preflight
# queries never prompt and never touch protected data. A previous revision
# spawned a Desktop-listing probe; do not bring that back (task #3205).
#
# Verdict: PASS means the probe's requests were attributed to
# com.ava.permissions-helper. The per-service preflight results are printed
# for information -- they reflect the helper identity's current grant state
# (0 granted / 1 denied / 2 not determined) and do NOT change the verdict.
#
# Prerequisites: the permissions helper is installed and running, and this
# macOS host permits `log show`. The script leaves its /tmp workdir in place
# and prints it on exit; it never removes evidence automatically.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="$REPO_ROOT/.venv/bin/python"
REQUESTED_WORKDIR="${1:-/tmp/tcc-spawn-chain-verify}"

if [[ ! -x "$PYTHON" ]]; then
    printf 'FAIL: repository venv python is unavailable: %s\n' "$PYTHON" >&2
    exit 2
fi

WORKDIR="$($PYTHON - "$REQUESTED_WORKDIR" <<'PY'
import sys
from pathlib import Path

print(Path(sys.argv[1]).resolve())
PY
)"
case "$WORKDIR" in
    # macOS resolves /tmp to /private/tmp, so both spellings must be accepted.
    /tmp/*|/private/tmp/*) ;;
    *)
        printf 'FAIL: workdir must resolve beneath /tmp: %s\n' "$WORKDIR" >&2
        exit 2
        ;;
esac
mkdir -p "$WORKDIR"
trap 'printf "workdir retained: %s\n" "$WORKDIR"' EXIT

PROBE="$WORKDIR/tcc_probe.py"
cat > "$PROBE" <<'PROBE_PY'
"""Preflight probe: side-effect-free TCC queries, spawned via the helper.

Writes {pid, ppid, results} as JSON to the path given in argv[1]. Every
TCCAccessPreflight call is recorded by tccd with this process's attribution,
which the invoking script reads back from the tccd log.
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
RESULT_WORDS = {0: "granted", 1: "denied", 2: "not-determined"}

_core_foundation = ctypes.CDLL(
    "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
)
_core_foundation.CFStringCreateWithCString.restype = ctypes.c_void_p
_core_foundation.CFStringCreateWithCString.argtypes = [
    ctypes.c_void_p,
    ctypes.c_char_p,
    ctypes.c_uint32,
]
_tcc = ctypes.CDLL("/System/Library/PrivateFrameworks/TCC.framework/Versions/A/TCC")
_tcc.TCCAccessPreflight.restype = ctypes.c_int
_tcc.TCCAccessPreflight.argtypes = [ctypes.c_void_p, ctypes.c_void_p]


def preflight(service: str) -> int:
    cf_service = _core_foundation.CFStringCreateWithCString(
        None, service.encode(), 0x08000100
    )
    return _tcc.TCCAccessPreflight(ctypes.c_void_p(cf_service), None)


def main() -> None:
    results = {}
    for service in SERVICES:
        code = preflight(service)
        results[service] = {"code": code, "result": RESULT_WORDS.get(code, "unknown")}
    with open(sys.argv[1], "w") as f:
        json.dump({"pid": os.getpid(), "ppid": os.getppid(), "results": results}, f, indent=2)
    # Stay alive briefly so the process can be inspected; the tccd records are
    # already written by the preflight calls above.
    time.sleep(3)


if __name__ == "__main__":
    main()
PROBE_PY

PROBE_PID="$($PYTHON - "$WORKDIR" "$PROBE" <<'PY'
import os
import sys
from pathlib import Path

from services.permissions_helper.client import spawn_process

workdir = Path(sys.argv[1])
probe = Path(sys.argv[2])
results_path = workdir / "probe.results.json"
result = spawn_process(
    f"tcc-spawn-chain-verify-{os.getpid()}",
    [sys.executable, str(probe), str(results_path)],
    dict(os.environ),
    str(workdir),
    str(workdir / "probe.stdout.log"),
    str(workdir / "probe.stderr.log"),
)
print(result["pid"])
PY
)"

if [[ ! "$PROBE_PID" =~ ^[0-9]+$ ]]; then
    printf 'FAIL: helper returned an invalid probe pid: %s\n' "$PROBE_PID" >&2
    exit 1
fi

sleep 2
LOG_OUTPUT="$(/usr/bin/log show --last 1m --style compact \
    --predicate 'eventMessage CONTAINS "AUTHREQ_ATTRIBUTION"' 2>&1)"
RESPONSIBLE_LINES="$(printf '%s\n' "$LOG_OUTPUT" \
    | grep -E 'responsible=\{[^}]*identifier=com\.ava\.permissions-helper' || true)"

if printf '%s\n' "$RESPONSIBLE_LINES" \
    | grep -Eq "(accessing|requesting)=\{[^}]*pid=${PROBE_PID}([^0-9]|$)"; then
    printf 'PASS: probe pid %s is attributed to com.ava.permissions-helper\n' "$PROBE_PID"
    if [[ -f "$WORKDIR/probe.results.json" ]]; then
        printf 'helper grant states (informational, not part of the verdict):\n'
        "$PYTHON" - "$WORKDIR/probe.results.json" <<'PY'
import json
import sys

data = json.load(open(sys.argv[1]))
for service, entry in data["results"].items():
    print(f'  {service}: {entry["result"]}')
PY
    else
        printf 'note: probe results file missing: %s (check probe.stderr.log)\n' \
            "$WORKDIR/probe.results.json"
    fi
    exit 0
fi

printf 'FAIL: no AUTHREQ_ATTRIBUTION line joined probe pid %s to the permissions helper\n' \
    "$PROBE_PID" >&2
printf '%s\n' "$LOG_OUTPUT" | tail -n 40 >&2
exit 1
