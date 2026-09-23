#!/usr/bin/env bash
# Serialize one heavy tool across host worktrees. CI independently enforces it.
set -euo pipefail

tool="${1:?usage: prepush-guard.sh pyright|tsc|eslint -- command...}"
shift
case "$tool" in pyright|tsc|eslint) ;; *) echo "Unknown heavy tool: $tool" >&2; exit 2 ;; esac
[[ "${1:-}" == -- && $# -ge 2 ]] || { echo "Expected -- command..." >&2; exit 2; }
shift

skip() {
    echo "WARNING: PRE-PUSH SKIPPED [$tool]: $*. CI must pass before merge." >&2
    exit 0
}

command -v flock >/dev/null || skip "flock is not installed"
command -v python3 >/dev/null || skip "python3 is not installed (load probe unavailable)"
case "$tool" in
    pyright)
        [[ -x .venv/bin/pyright ]] || skip "missing .venv/bin/pyright; run env -u VIRTUAL_ENV uv sync"
        ;;
    tsc|eslint)
        [[ -d ui/web/node_modules ]] || skip "missing ui/web/node_modules; run (cd ui/web && npm ci)"
        command -v node >/dev/null || skip "node is not installed"
        command -v npm >/dev/null || skip "npm is not installed"
        if [[ "$tool" == tsc ]]; then
            required_bins=(next tsc)
        else
            required_bins=(eslint)
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
if ! flock -n 9; then
    echo "pre-push: waiting for $tool lock (up to ${wait_seconds}s)..." >&2
    flock -w "$wait_seconds" 9 || skip "lock wait timed out after ${wait_seconds}s ($lock_file)"
fi
check_load
echo "pre-push: running $tool"
# Keep fd 9 inherited until the tool exits; preserve its real failure status.
exec "$@"
