#!/usr/bin/env bash
# Convergence guard for the editable-venv write window: a production-scoped
# `uv sync` that reinstalls the editable package must survive a hardened venv
# whose site-packages AND ava-*.dist-info directories are both 0o555 — exactly
# the state converge's protection leaves on a host — and must restore those
# exact original modes afterwards.
#
# Regression for the 2026-09-03 rollout (adcc62b wave): editable_pth_write_window
# opened the site-packages root and the .pth / direct_url.json records, but NOT
# the ava-*.dist-info directory, so uv's reinstall uninstall failed mid-sync
# with `failed to remove .../INSTALLER: Permission denied`; the failure deleted
# the .venv/bin/ava launcher and stranded hosts on old code until the watchdog
# backoff expired.
#
# Usage:
#   scripts/test_uv_sync_write_window.sh
#
# Requires uv on PATH. Runs against a throwaway local project; only the
# hatchling build dependency is fetched from the network.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

PROBE_DIR="$(mktemp -d "${TMPDIR:-/tmp}/ava-uv-sync-window.XXXXXX")"
trap 'if [ -d "$PROBE_DIR" ]; then chmod -R u+w "$PROBE_DIR" 2>/dev/null || true; rm -rf "$PROBE_DIR"; fi' EXIT

uv run python - "$PROBE_DIR" <<'PY'
import stat
import subprocess
import sys
from pathlib import Path

probe = Path(sys.argv[1])
(probe / "pyproject.toml").write_text(
    "\n".join(
        (
            "[project]",
            "name = \"ava\"",
            "version = \"0.1.5.dev0\"",
            "requires-python = \">=3.10\"",
            "dependencies = []",
            "",
            "[build-system]",
            "requires = [\"hatchling\"]",
            "build-backend = \"hatchling.build\"",
            "",
        )
    )
)
(probe / "src" / "ava").mkdir(parents=True)
(probe / "src" / "ava" / "__init__.py").write_text("__version__ = '0.1.5.dev0'\n")


def uv(*args: str) -> None:
    subprocess.run(["uv", *args], cwd=probe, check=True)


uv("lock")
uv("sync", "--no-dev")

site_packages = next(iter(sorted(probe.glob(".venv/lib/python*/site-packages"))))
dist_infos = sorted(site_packages.glob("ava-*.dist-info"))
assert len(dist_infos) == 1, f"expected one ava dist-info directory, found {dist_infos}"
dist_info = dist_infos[0]

# Simulate converge's post-sync protection: harden the whole uv write surface.
site_packages.chmod(0o555)
dist_info.chmod(0o555)

from cli.commands._update_uv_sync import run_uv_sync

result = run_uv_sync(probe, reinstall_package="ava")
if result.returncode != 0:
    raise SystemExit(f"hardened-venv uv sync failed (rc={result.returncode})")
assert (
    stat.S_IMODE(site_packages.stat().st_mode) == 0o555
), "site-packages protection was not restored"
assert (
    stat.S_IMODE(dist_info.stat().st_mode) == 0o555
), "ava dist-info protection was not restored"
assert (dist_info / "INSTALLER").is_file(), "reinstall did not complete"
print("OK: hardened venv survived reinstall sync; 0o555 modes restored")
PY
