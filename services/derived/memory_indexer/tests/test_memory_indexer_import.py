"""Cold-import boundary guards for the memory indexer daemon."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).parents[4]
_PROBE = Path(__file__).with_name("child_daemon_import.py")
_BACKEND_PREFIX = "services.derived.memory_indexer.backends."
_CONCRETE_BACKENDS = {
    f"{_BACKEND_PREFIX}numpy",
    f"{_BACKEND_PREFIX}pgvector",
}


def _run_fresh_daemon_import(tmp_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Import the daemon in a fresh interpreter with an isolated unit home."""
    (tmp_path / ".env").write_text(
        "MEIVA_MACHINE_NAME=test-box\n"
        "MEIVA_MACHINE_SERVE_GATEWAY=true\n"
        "MEIVA_MACHINE_SERVE_AGENT_RUNNER=true\n"
        "MEIVA_DB_URL=postgresql://unprovisioned@127.0.0.1:1/unprovisioned\n"
        "MEIVA_REDIS_URL=redis://127.0.0.1:1/0\n"
        "MEIVA_CLUSTER_SECRET=test-cluster-secret\n"
        "MEIVA_OS_JOBS_ENABLED=false\n"
        "MEIVA_TELEMETRY_OTLP_ENABLED=false\n"
        "MEIVA_PGBOUNCER_ENABLED=false\n"
        "MEIVA_TRANSPORT_ENCRYPTION=overlay\n"
    )
    env = os.environ.copy()
    env["MEIVA_HOME"] = str(tmp_path)
    env["MEIVA_CONFIG_FETCH"] = "skip"
    env.pop("MEIVA_PROCESS_PROFILE", None)
    env.pop("MEIVA_AGENT_ID", None)
    return subprocess.run(  # noqa: S603 — fixed interpreter and repository-owned code
        [sys.executable, str(_PROBE), *args],
        cwd=_REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )


def test_daemon_import_does_not_load_backend_implementations(tmp_path: Path) -> None:
    """Importing the daemon must leave every concrete backend unselected."""
    result = _run_fresh_daemon_import(tmp_path, "backends", _BACKEND_PREFIX)

    imported = set(json.loads(result.stdout))
    assert imported.isdisjoint(_CONCRETE_BACKENDS)


def test_daemon_cold_import_completes_within_updater_budget(tmp_path: Path) -> None:
    """The daemon import must remain comfortably inside the updater timeout."""
    result = _run_fresh_daemon_import(tmp_path, "timing")

    # 20s leaves broad host variance while catching the 38–52s eager-import regression
    # before the updater's 30s timeout is reached.
    assert float(result.stdout) < 20
