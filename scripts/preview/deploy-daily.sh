#!/usr/bin/env bash
# Daily deploy for preview cluster — pulls latest main, restarts, validates.
# Run by cron at 13:45 Pacific (20:45 UTC).
set -euo pipefail

CLUSTER="preview"
AVA_REPO="${AVA_REPO:-$HOME/Ava}"
CLUSTER_HOME="$HOME/.ava-preview"
LOG_DIR="$AVA_REPO/logs"

mkdir -p "$LOG_DIR"
exec > >(tee -a "$LOG_DIR/preview-deploy.log") 2>&1

echo "=== $(date -u +'%Y-%m-%dT%H:%M:%SZ') Daily deploy for $CLUSTER ==="

export PATH="$HOME/.local/bin:$PATH"
# Identity is the home path — AVA_HOME alone selects the preview cluster.
export AVA_HOME="$CLUSTER_HOME"

# Source cluster .env
if [ -f "$CLUSTER_HOME/.env" ]; then
    set -a; source "$CLUSTER_HOME/.env"; set +a
else
    echo "FATAL: $CLUSTER_HOME/.env not found"
    exit 1
fi

# ----- 1. Ensure infra -----
pg_isready -h localhost -p 5432 >/dev/null 2>&1 || {
    sudo -u postgres /usr/lib/postgresql/17/bin/pg_ctl -D /var/lib/postgresql/17/data -l /var/log/postgresql/pg.log start
    sleep 2
}
redis-cli ping >/dev/null 2>&1 || {
    sudo redis-server --daemonize yes --logfile /var/log/redis.log
    sleep 1
}

# ----- 2. Pull latest main -----
echo ""
echo "--- Pulling main ---"
cd "$AVA_REPO"
git fetch origin main 2>&1
LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse origin/main)

if [ "$LOCAL" != "$REMOTE" ]; then
    echo "Updating $LOCAL -> $REMOTE"
    git checkout main && git pull origin main && uv sync 2>&1
else
    echo "Already at latest ($LOCAL)."
fi

# ----- 3. Restart -----
echo ""
echo "--- Restarting ---"
cd "$AVA_REPO"
ava restart 2>&1 || { ava stop -y --keep-infra 2>&1 || true; sleep 3; ava start 2>&1; }

# ----- 4. Validate -----
echo ""
echo "--- Validation suite ---"
bash "$AVA_REPO/scripts/preview/validate.sh" 2>&1 || echo "Validation errors (non-fatal)"

# ----- 5. Sample agents -----
echo ""
echo "--- Sample agents ---"
bash "$AVA_REPO/scripts/preview/spawn-samples.sh" 2>&1 || echo "Sample errors (non-fatal)"

echo ""
echo "=== $(date -u +'%Y-%m-%dT%H:%M:%SZ') Deploy complete ==="
