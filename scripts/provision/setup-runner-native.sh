#!/usr/bin/env bash
# Register a GitHub Actions self-hosted runner that runs CI jobs DIRECTLY on a
# native Linux host (no container, no Docker) — the de-containerized CI lane.
# The job steps run as the non-root `ghrunner` user; the test suite spins
# throwaway native pg/redis clusters (tests/_containers.py).
#
# Prereq: install-system.sh + install-playwright.sh already ran on this host.
# Run as root ON the host:
#   export GITHUB_REPOSITORY=your-org/Ava
#   REG=$(gh api -X POST repos/$GITHUB_REPOSITORY/actions/runners/registration-token --jq .token)
#   bash setup-runner-native.sh ci-1 ava-ci-native "$REG"
#
# Idempotent for the runner user; re-running with a fresh token re-registers.
set -euo pipefail

RUNNER_NAME="${1:?usage: setup-runner-native.sh <runner-name> <labels> <reg-token>}"
LABELS="${2:?missing labels}"
REG_TOKEN="${3:?missing registration token}"

RUNNER_VERSION="2.335.0"
REPO_URL="https://github.com/${GITHUB_REPOSITORY:?set GITHUB_REPOSITORY=owner/repo (e.g. your-org/Ava)}"
RUNNER_USER="ghrunner"
BASE="/opt/ava-ci/${RUNNER_NAME}"
ARCH="x64"   # CPX = x86_64; CAX (arm64) would use linux-arm64

id "$RUNNER_USER" >/dev/null 2>&1 || useradd -m -s /bin/bash "$RUNNER_USER"
mkdir -p "$BASE"
chown -R "$RUNNER_USER":"$RUNNER_USER" /opt/ava-ci

# Persistent CI caches (ci.yml points UV_CACHE_DIR / PIP_CACHE_DIR /
# PRE_COMMIT_HOME / npm_config_cache here). The runner user must own them or the
# first job's writes fail. Shared across runs on this single-owner host — the
# cache-poisoning risk from PR-supplied code is accepted (see ci.yml). Playwright
# is cached here; chromium lives under playwright/ (per CI job's PLAYWRIGHT_BROWSERS_PATH).
mkdir -p /var/cache/ava-ci/{uv,pip,npm,pre-commit,playwright}
chown -R "$RUNNER_USER":"$RUNNER_USER" /var/cache/ava-ci

if [ ! -f "$BASE/config.sh" ]; then
  sudo -u "$RUNNER_USER" bash -c "cd '$BASE' && \
    curl -fsSLO 'https://github.com/actions/runner/releases/download/v${RUNNER_VERSION}/actions-runner-linux-${ARCH}-${RUNNER_VERSION}.tar.gz' && \
    tar xzf 'actions-runner-linux-${ARCH}-${RUNNER_VERSION}.tar.gz' && \
    rm -f 'actions-runner-linux-${ARCH}-${RUNNER_VERSION}.tar.gz'"
  (cd "$BASE" && ./bin/installdependencies.sh)
fi

# Runner job environment: pg server binaries are off-PATH on Debian/Ubuntu, and
# Playwright's browser lives in the shared path. `.path` prepends to PATH, `.env`
# sets job env vars — both read by the runner per job.
# PLAYWRIGHT_BROWSERS_PATH matches ci.yml's job env (/var/cache/ava-ci/playwright):
# the .env value is dead config overridden per-job, so keep it in sync rather
# than stale at /opt/ms-playwright (audit round-2 tests-ci P3).
printf '%s\n' "/usr/lib/postgresql/17/bin" "/usr/local/bin" "/usr/bin" "/bin" > "$BASE/.path"
printf '%s\n' "PLAYWRIGHT_BROWSERS_PATH=/var/cache/ava-ci/playwright" "LANG=en_US.UTF-8" "LC_ALL=en_US.UTF-8" > "$BASE/.env"
chown "$RUNNER_USER":"$RUNNER_USER" "$BASE/.path" "$BASE/.env"

# ── Preflight ────────────────────────────────────────────────────────────────
# Same check the autoscale channel runs before registering a host
# (ops/ci_autoscale/cloud-init.yml): the static-runner channel had no tool
# verification, so a broken box (0-byte uv, missing pgbouncer) registered
# anyway and every job failed — or, for a do-nothing binary, silently skipped
# work while staying green (audit round-2 tests-ci P2).
#
# Each tool must SUCCEED **and** PRINT a version, as the runner user with the
# runner's own PATH. Neither half is enough on its own: exit status alone
# lets a 0-byte executable (empty script, exit 0) through, and output alone
# is faked by a missing binary's `command not found` stderr — so capture
# stdout ONLY, require zero exit, require non-empty stdout.
for tool in "uv --version" "node --version" "npm --version" \
            "initdb --version" "redis-server --version" "pgbouncer --version" \
            "git --version"; do
  out=$(sudo -u "$RUNNER_USER" \
          env PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/usr/lib/postgresql/17/bin" \
          bash -c "$tool" 2>/dev/null) || { echo "PREFLIGHT: '$tool' exited non-zero — refusing to register"; exit 1; }
  [ -n "$out" ] || { echo "PREFLIGHT: '$tool' exited 0 but printed nothing — refusing to register"; exit 1; }
done

sudo -u "$RUNNER_USER" bash -c "cd '$BASE' && ./config.sh \
  --url '$REPO_URL' --token '$REG_TOKEN' \
  --name '$RUNNER_NAME' --labels '$LABELS' \
  --unattended --replace"

./svc.sh install "$RUNNER_USER" 2>/dev/null || (cd "$BASE" && ./svc.sh install "$RUNNER_USER")
(cd "$BASE" && ./svc.sh start)
sleep 2
(cd "$BASE" && ./svc.sh status | head -15)
