#!/usr/bin/env bash
# Serialize one heavy tool across host worktrees. CI independently enforces it.
set -euo pipefail

tool="${1:?usage: prepush-guard.sh pyright|tsc|eslint|vitest -- command...}"
shift
case "$tool" in pyright|tsc|eslint|vitest) ;; *) echo "Unknown heavy tool: $tool" >&2; exit 2 ;; esac
[[ "${1:-}" == -- && $# -ge 2 ]] || { echo "Expected -- command..." >&2; exit 2; }
shift

skip() {
    echo "WARNING: PRE-PUSH SKIPPED [$tool]: $*. CI must pass before merge." >&2
    exit 0
}

command -v python3 >/dev/null || skip "python3 is not installed (load/lock probe unavailable)"
case "$tool" in
    pyright)
        [[ -x .venv/bin/pyright ]] || skip "missing .venv/bin/pyright; run env -u VIRTUAL_ENV uv sync"
        ;;
    tsc|eslint|vitest)
        [[ -d ui/web/node_modules ]] || skip "missing ui/web/node_modules; run (cd ui/web && npm ci)"
        command -v node >/dev/null || skip "node is not installed"
        command -v npm >/dev/null || skip "npm is not installed"
        if [[ "$tool" == tsc ]]; then
            required_bins=(next tsc)
        elif [[ "$tool" == eslint ]]; then
            required_bins=(eslint)
        else
            required_bins=(vitest)
        fi
        for bin in "${required_bins[@]}"; do
            [[ -x "ui/web/node_modules/.bin/$bin" ]] || skip "missing frontend executable $bin; run (cd ui/web && npm ci)"
        done
        ;;
esac

# 120s lets a normal ~108s pyright finish without an unbounded push wait.
wait_seconds="${AVA_PREPUSH_LOCK_WAIT_SECONDS:-120}"
[[ "$wait_seconds" =~ ^[0-9]+([.][0-9]+)?$ ]] || { echo "Invalid AVA_PREPUSH_LOCK_WAIT_SECONDS: $wait_seconds" >&2; exit 2; }

check_load() {
    local reason
    # 1.5 runnable tasks/core tolerates short bursts but avoids adding work to
    # a sustained CPU queue. Check again after waiting for a lock.
    reason="$(python3 - "${AVA_PREPUSH_MAX_LOAD_PER_CORE:-1.5}" <<'PY'
import math
import os
import sys

threshold = float(sys.argv[1])
if not math.isfinite(threshold) or threshold < 0:
    raise SystemExit("AVA_PREPUSH_MAX_LOAD_PER_CORE must be finite and nonnegative")
per_core = os.getloadavg()[0] / (os.cpu_count() or 1)
if per_core > threshold:
    print(f"1-minute load/core {per_core:.2f} exceeds threshold {threshold:g}")
PY
    )" || skip "load probe failed; check AVA_PREPUSH_MAX_LOAD_PER_CORE and host load support"
    [[ -z "$reason" ]] || skip "$reason"
}

check_load
# A fixed host path, independent of HOME, TMPDIR, git-dir, and checkout. Sticky
# directory + non-truncating creation lets different users share the same locks.
lock_dir="${AVA_PREPUSH_LOCK_DIR:-/tmp/ava-prepush-locks}"
[[ ! -L "$lock_dir" ]] || skip "lock directory is a symlink: $lock_dir"
mkdir -m 1777 "$lock_dir" 2>/dev/null || [[ -d "$lock_dir" ]] || skip "cannot create lock directory $lock_dir"
lock_file="$lock_dir/$tool.lock"
[[ ! -L "$lock_file" ]] || skip "lock file is a symlink: $lock_file"
(umask 000; set -o noclobber; : > "$lock_file") 2>/dev/null || [[ -f "$lock_file" ]] || skip "cannot create lock $lock_file"
exec 9<"$lock_file" || skip "cannot open lock $lock_file"

# No `flock(1)` binary on this host's platform (notably stock macOS) --
# fcntl.flock(2) on the fd bash just opened does the exact same job. The lock
# is bound to the OPEN FILE DESCRIPTION fd 9 refers to, not to the python3
# process that requests it: once acquired, it stays held for as long as ANY
# descriptor referencing that same open file description remains open —
# python3 exiting after a successful acquire does not release it, because fd
# 9 is still open in this shell and inherited by the `exec "$@"` below.
# LOCK_EX|LOCK_NB never blocks; a wait is polling LOCK_NB in a loop, since
# fcntl has no built-in timed blocking wait like flock(1)'s -w.
try_lock() {
    local timeout="$1"
    python3 - "$timeout" <<'PY'
import fcntl
import sys
import time

timeout = float(sys.argv[1])
deadline = time.monotonic() + timeout
poll_interval = 0.1
while True:
    try:
        fcntl.flock(9, fcntl.LOCK_EX | fcntl.LOCK_NB)
        sys.exit(0)
    except BlockingIOError:
        if timeout <= 0 or time.monotonic() >= deadline:
            sys.exit(1)
        time.sleep(min(poll_interval, max(0.0, deadline - time.monotonic())))
    except OSError as error:
        print(f"lock probe failed: {error}", file=sys.stderr)
        sys.exit(2)
PY
}

# `cmd || status=$?` is the set -e-safe way to capture an exit code: the `||`
# branch only runs on failure, and `$?` there is still try_lock's own status
# (nothing else has run yet) -- unlike `if ! try_lock; then ... $?; fi`, where
# `$?` inside the then-branch reflects the (already-negated) `if` test, not
# try_lock's original code, so 1 (lock held) and 2 (probe error) could not be
# told apart.
status=0
try_lock 0 || status=$?
if [[ "$status" != 0 ]]; then
    [[ "$status" == 2 ]] && skip "lock probe failed; check python3's fcntl support"
    echo "pre-push: waiting for $tool lock (up to ${wait_seconds}s)..." >&2
    status=0
    try_lock "$wait_seconds" || status=$?
    if [[ "$status" != 0 ]]; then
        [[ "$status" == 2 ]] && skip "lock probe failed; check python3's fcntl support"
        skip "lock wait timed out after ${wait_seconds}s ($lock_file)"
    fi
fi
check_load
echo "pre-push: running $tool"
# Keep fd 9 inherited until the tool exits; preserve its real failure status.
exec "$@"
