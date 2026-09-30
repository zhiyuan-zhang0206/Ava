#!/usr/bin/env bash
# Install Playwright + the Chromium browser into a shared, world-readable path
# (/opt/ms-playwright) so every job/user on the host finds it. Run as root
# (--with-deps installs OS libs). The verification image does not run it.
#
# PLAYWRIGHT_VERSION must stay in sync with the playwright dep in pyproject.toml /
# uv.lock (a mismatched chromium build fails at launch).
set -euo pipefail
PLAYWRIGHT_VERSION="${PLAYWRIGHT_VERSION:-1.59.0}"
export PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright

pip install --break-system-packages "playwright==${PLAYWRIGHT_VERSION}"
playwright install --with-deps chromium
chmod -R a+rX /opt/ms-playwright
