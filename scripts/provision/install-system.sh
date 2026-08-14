#!/usr/bin/env bash
# The system layer of a Linux Ava host (a CI runner / bench box): Python 3.12 +
# build tools, then the shared provision pieces — pg17 + redis servers, node 22,
# CLI tools. Composes scripts/provision/* so the package logic lives in one place
# (install.sh's gateway path and this CI path draw from the same pieces).
#
# Server binaries only, NO initdb: the test suite spins throwaway native clusters
# (tests/_containers.py), and a gateway unit's own per-cluster instance is
# provisioned under $AVA_HOME/pg by `ava start` (install.sh --role gateway).
#
# Idempotent, runs as root (or via sudo). macOS prod is provisioned via brew.
set -euo pipefail

if ! command -v apt-get >/dev/null 2>&1; then
  echo "install-system.sh: Debian/Ubuntu only (apt-get); macOS uses brew." >&2
  exit 2
fi
export DEBIAN_FRONTEND=noninteractive
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Python 3.12 + build tools — the base layer the pieces sit on.
apt-get update
apt-get install -y --no-install-recommends \
  python3.12 python3.12-venv python3.12-dev python3-pip \
  build-essential ca-certificates curl gnupg locales

# Shared provision pieces: pg17 + redis servers (no initdb), node 22, CLI tools.
bash "${HERE}/database.sh"
bash "${HERE}/node.sh"
bash "${HERE}/../install-cli-tools.sh"
