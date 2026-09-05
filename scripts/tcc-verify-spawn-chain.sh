#!/usr/bin/env bash
# Verify TCC attribution after PR-1/PR-2 and this spawn-chain change are deployed.
# Prerequisites: the permissions helper is installed and running, and this macOS
# host permits `log show`. The script leaves its /tmp workdir in place and prints
# it on exit; it never removes evidence automatically.

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
    /tmp/*) ;;
    *)
        printf 'FAIL: workdir must resolve beneath /tmp: %s\n' "$WORKDIR" >&2
        exit 2
        ;;
esac
mkdir -p "$WORKDIR"
trap 'printf "workdir retained: %s\n" "$WORKDIR"' EXIT

PROBE="$WORKDIR/tcc_probe.py"
printf '%s\n' \
    "import os,time,sys; print(os.listdir(os.path.expanduser('~/Desktop'))); time.sleep(float(sys.argv[1]))" \
    >"$PROBE"

PROBE_PID="$($PYTHON - "$WORKDIR" "$PROBE" <<'PY'
import os
import sys
from pathlib import Path

from services.permissions_helper.client import spawn_process

workdir = Path(sys.argv[1])
probe_source = Path(sys.argv[2]).read_text()
result = spawn_process(
    f"tcc-spawn-chain-verify-{os.getpid()}",
    [sys.executable, "-c", probe_source, "30"],
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
    | grep -Eq "accessing=\\{[^}]*pid=${PROBE_PID}([^0-9]|$)"; then
    printf 'PASS: probe pid %s is attributed to com.ava.permissions-helper\n' "$PROBE_PID"
    exit 0
fi

printf 'FAIL: no AUTHREQ_ATTRIBUTION line joined probe pid %s to the permissions helper\n' \
    "$PROBE_PID" >&2
printf '%s\n' "$LOG_OUTPUT" | tail -n 40 >&2
exit 1
